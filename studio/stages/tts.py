"""Stage 3: narration. Kokoro-82M via mlx-audio (doc 14 §4), or a mock engine built on
`ffmpeg -f lavfi` (sine tone or silence) whose length is estimated at 150 wpm.

Writes narration.wav plus timings.json (start/end of the hook, each beat and the CTA),
which the captions and render stages use to time words and images to the narration.
The kokoro engine also writes tts_words.json (Kokoro's own per-word timings), used by the
cheaper `kokoro-timestamps` captions engine.
"""

from __future__ import annotations

import gc
import json
import math
import os
import sys
import wave
from pathlib import Path
from typing import Any

from studio.contracts import BrandPack, Script, VideoJob
from studio.stages.base import (
    EngineUnavailable,
    StageContext,
    StageError,
    hash_inputs,
    is_cached,
    log,
    media_duration,
    run_ffmpeg,
    write_cache,
)

STAGE = "tts"
VERSION = 5  # 5: British English voices (bf_*/bm_*) with lang_code 'b'
SAMPLE_RATE = 24_000
WPM = 150
GAP_S = 0.08  # "cut the air" (06 §3): only a breath between segments
KOKORO_MODEL = os.environ.get("STUDIO_KOKORO_MODEL", "mlx-community/Kokoro-82M-bf16")

# US-English voices in mlx-community/Kokoro-82M-bf16/voices/*.safetensors (HF API, 2026-10-16).
# A blend is "a,b" (mlx-audio averages the embeddings).
KOKORO_US_VOICES = frozenset({
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole",
    "af_nova", "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir",
    "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa",
})
# British-English voices (same repo; Kokoro lang_code "b", misaki en-GB G2P). For the UK/EU/AU
# kids pilot (doc 26). Grades per hexgrad VOICES.md: bf_emma B-, bf_isabella C, bm_george C,
# bm_fable C, the rest D.
KOKORO_GB_VOICES = frozenset({
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bm_daniel", "bm_fable", "bm_george",
    "bm_lewis",
})
KOKORO_VOICES = KOKORO_US_VOICES | KOKORO_GB_VOICES

# Grades from the Kokoro-82M model card (hexgrad/Kokoro-82M VOICES.md, "overall grade",
# target quality x training duration). af_heart and af_bella are the only A-range US voices;
# the best US male voices (am_michael, am_fenrir, am_puck) are C+. Brands keep their voice;
# a weak one is logged so it can be swapped in the brand pack (or with STUDIO_TTS_VOICE).
VOICE_GRADES = {
    "af_heart": "A", "af_bella": "A-", "af_nicole": "B-", "af_aoede": "C+", "af_kore": "C+",
    "af_sarah": "C+", "af_nova": "C", "af_alloy": "C", "af_sky": "C-", "af_jessica": "D",
    "af_river": "D", "am_michael": "C+", "am_fenrir": "C+", "am_puck": "C+", "am_echo": "D",
    "am_eric": "D", "am_liam": "D", "am_onyx": "D", "am_santa": "D-", "am_adam": "F+",
}
RECOMMENDED_VOICES = ("af_heart", "af_bella", "am_michael", "am_fenrir", "am_puck")
# doc 06 §3 "cut the air": an AI voice at 1.0 drags. Brand speeds below this are raised to it.
ENERGY_SPEED = 1.07
MAX_SPEED = 1.25

_kokoro: Any = None  # resident KokoroPipeline (holds the model), one per process / batch stage
_kokoro_lang: str = ""  # lang_code of the resident pipeline


def check_voice(voice: str) -> None:
    names = [v.strip() for v in voice.split(",")]
    unknown = [v for v in names if v not in KOKORO_VOICES]
    if unknown and os.environ.get("STUDIO_KOKORO_ANY_VOICE") != "1":
        raise StageError(f"tts: unknown Kokoro voice(s) {unknown}; choose from "
                         f"{sorted(KOKORO_VOICES)} (STUDIO_KOKORO_ANY_VOICE=1 skips this)")
    if len({lang_code(v) for v in names}) > 1:
        raise StageError(f"tts: can't blend US and British voices in one delivery: {voice!r}")


def lang_code(voice: str) -> str:
    """Kokoro pipeline language: 'b' (British English) for bf_/bm_ voices, else 'a' (US)."""
    return "b" if voice.split(",")[0].strip().startswith("b") else "a"


def delivery(brand: BrandPack) -> tuple[str, float]:
    """(voice, speed) actually used: STUDIO_TTS_VOICE / STUDIO_TTS_SPEED override the brand;
    otherwise the brand's voice at max(brand speed, ENERGY_SPEED), capped at MAX_SPEED."""
    voice = os.environ.get("STUDIO_TTS_VOICE", "").strip() or brand.voice.voice
    raw = os.environ.get("STUDIO_TTS_SPEED", "").strip()
    if raw:
        try:
            speed = float(raw)
        except ValueError as exc:
            raise StageError(f"tts: STUDIO_TTS_SPEED={raw!r} is not a number") from exc
    else:
        speed = max(brand.voice.speed, ENERGY_SPEED)
    if not math.isfinite(speed):
        raise StageError("tts: STUDIO_TTS_SPEED must be finite")
    return voice, round(min(max(speed, 0.5), MAX_SPEED if not raw else 2.0), 3)


def voice_grade(voice: str) -> str:
    """Model-card grade of a voice id; a blend gets its weakest member's grade."""
    grades = [VOICE_GRADES.get(v.strip(), "?") for v in voice.split(",")]
    order = ["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F+", "F", "?"]
    return max(grades, key=lambda g: order.index(g) if g in order else len(order))


def segments(script: Script) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = [{"kind": "hook", "beat": 0, "text": script.hook}]
    segs += [{"kind": "beat", "beat": i, "text": b.text} for i, b in enumerate(script.beats)]
    if script.cta:
        segs.append({"kind": "cta", "beat": len(script.beats) - 1, "text": script.cta})
    return [s for s in segs if s["text"].strip()]


def estimate_seconds(text: str, speed: float = 1.0) -> float:
    words = len(text.split())
    return max(0.8, words / WPM * 60 / max(speed, 0.1))


# ---------------------------------------------------------------- engines


def _mock(segs: list[dict[str, Any]], out: Path, speed: float, tone: bool) -> list[float]:
    durations = [round(estimate_seconds(s["text"], speed), 3) for s in segs]
    total = sum(durations) + GAP_S * (len(segs) - 1)
    src = (
        f"sine=frequency=220:sample_rate={SAMPLE_RATE}:duration={total:.3f}"
        if tone
        else f"anullsrc=r={SAMPLE_RATE}:cl=mono"
    )
    args = ["-f", "lavfi", "-i", src, "-t", f"{total:.3f}", "-ac", "1", "-ar", str(SAMPLE_RATE)]
    if tone:
        args += ["-af", "volume=0.08"]
    run_ffmpeg([*args, "-c:a", "pcm_s16le", out.name], cwd=out.parent)
    return durations


def _load_kokoro(lang: str = "a") -> Any:
    """Load Kokoro once per process (or per batch stage) and return its English pipeline.

    Verified against mlx-audio 0.5.9: `mlx_audio.tts.utils.load(repo)` returns the Kokoro
    `Model` (sample_rate 24000). Its `generate()` yields audio only, so we drive
    `KokoroPipeline` directly: it yields `Result(graphemes, phonemes, tokens, output)` where
    each misaki token carries `start_ts`/`end_ts` (seconds, from the predicted durations
    that produced the audio) and `whitespace`. G2P is misaki + spacy en_core_web_sm.
    """
    global _kokoro, _kokoro_lang
    if _kokoro is not None and _kokoro_lang != lang:
        unload()  # one resident pipeline; switching US <-> British reloads it
    if _kokoro is None:
        try:  # lazy: heavy, optional (local-audio extra)
            from mlx_audio.tts.models.kokoro.pipeline import KokoroPipeline
            from mlx_audio.tts.utils import load
        except ImportError as exc:
            raise EngineUnavailable(
                "TTS engine 'kokoro' needs mlx-audio, which is not installed. Install the "
                "local-audio extra (`uv sync --extra local-audio`) or run with --mock / "
                "--engine tts=mock."
            ) from exc
        try:
            model = load(KOKORO_MODEL)
            pipeline = KokoroPipeline(lang_code=lang, model=model, repo_id=KOKORO_MODEL)
        except ImportError as exc:  # misaki / spacy model missing
            raise EngineUnavailable(f"TTS engine 'kokoro': G2P not installed ({exc}). "
                                    "Run `uv sync --extra local-audio`.") from exc
        _kokoro, _kokoro_lang = pipeline, lang
    return _kokoro


def _segment_words(results: list[Any], rate: int) -> list[dict[str, Any]]:
    """Word timings (relative to the segment start) from Kokoro's per-token timestamps.

    Each pipeline chunk restarts its clock at 0, so chunk offsets are accumulated.
    Punctuation tokens (no whitespace before them) are glued onto the previous word.
    """
    words: list[dict[str, Any]] = []
    offset = 0.0
    for r in results:
        glue = False
        for tok in getattr(r, "tokens", None) or []:
            text = str(getattr(tok, "text", "")).strip()
            start, end = getattr(tok, "start_ts", None), getattr(tok, "end_ts", None)
            if text and (start is None or end is None):  # untimed (e.g. OOD word) token
                start = end = (words[-1]["end"] - offset) if words else 0.0
            if text:
                if glue and words:
                    words[-1]["text"] += text
                    words[-1]["end"] = round(offset + end, 3)
                else:
                    words.append({"text": text, "start": round(offset + start, 3),
                                  "end": round(offset + end, 3)})
            glue = not getattr(tok, "whitespace", " ")
        audio = getattr(r, "audio", None)
        offset += (audio.size if audio is not None else 0) / rate
    return words


def _kokoro_tts(
    segs: list[dict[str, Any]], out: Path, voice: str, speed: float
) -> tuple[list[float], list[dict[str, Any]]]:
    """Synthesise each segment; return per-segment durations and absolute word timings."""
    check_voice(voice)
    pipeline = _load_kokoro(lang_code(voice))  # raises EngineUnavailable if mlx-audio is missing
    import numpy as np  # installed with mlx-audio

    rate = int(getattr(pipeline.model, "sample_rate", SAMPLE_RATE))
    gap = np.zeros(int(GAP_S * rate), dtype="<i2").tobytes()
    durations, words = [], []
    total_frames = 0
    tmp = out.with_suffix(".tmp.wav")
    try:
        with wave.open(str(tmp), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            for i, seg in enumerate(segs):
                segment_frames = 0
                try:
                    for result in pipeline(seg["text"], voice=voice, speed=speed):
                        if result.audio is None:
                            del result
                            _clear_mlx_cache()
                            continue
                        audio = np.asarray(result.audio, dtype=np.float32).reshape(-1)
                        offset = total_frames / rate
                        for word in _segment_words([result], rate):
                            words.append({**word, "start": round(offset + word["start"], 3),
                                          "end": round(offset + word["end"], 3)})
                        wav.writeframesraw((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
                        segment_frames += len(audio)
                        total_frames += len(audio)
                        # Release chunk buffers before clearing Metal's allocator cache.
                        del audio, result
                        _clear_mlx_cache()
                except (FileNotFoundError, ValueError, RuntimeError) as exc:
                    raise StageError(
                        f"tts: kokoro failed for voice {voice!r} ({KOKORO_MODEL}): {exc}"
                    ) from exc
                finally:
                    _clear_mlx_cache()
                if not segment_frames:
                    raise StageError("tts: kokoro returned no audio for a narration segment")
                durations.append(segment_frames / rate)
                if i < len(segs) - 1:
                    wav.writeframesraw(gap)
                    total_frames += len(gap) // 2
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return durations, words


def _clear_mlx_cache() -> None:
    # Mock runs and cleanup must never initialise Metal merely to release it.
    mx = sys.modules.get("mlx.core")
    if mx is not None:
        mx.clear_cache()


def unload() -> None:
    """Free the resident TTS model (run_batch calls this after the TTS stage)."""
    global _kokoro, _kokoro_lang
    _kokoro, _kokoro_lang = None, ""
    gc.collect()
    _clear_mlx_cache()


# ---------------------------------------------------------------- stage


def run(job: VideoJob, brand: BrandPack, ctx: StageContext) -> VideoJob:
    if job.script is None:
        raise StageError("tts: no script")
    workdir = Path(job.workdir)
    audio, timings = workdir / "narration.wav", workdir / "timings.json"
    words_path = workdir / "tts_words.json"  # Kokoro's own word timings (kokoro engine only)
    engine = ctx.engine("tts")
    segs = segments(job.script)
    if not segs:
        raise StageError("tts: no narration text")
    voice, speed = delivery(brand)
    key = hash_inputs(STAGE, VERSION, engine, brand.voice, voice, speed, segs,
                      KOKORO_MODEL if engine == "kokoro" else None)
    expected = [audio, timings] + ([words_path] if engine == "kokoro" else [])
    if is_cached(job, STAGE, key, expected, ctx):
        job.audio_path = audio
        log(job, "tts: cached, skipped")
        return job

    words: list[dict[str, Any]] | None = None
    if engine in ("mock", "silence"):
        durations = _mock(segs, audio, speed, tone=engine == "mock")
    elif engine == "kokoro":
        grade = voice_grade(voice)
        if grade[0] not in "AB":
            log(job, f"tts: voice {voice} is grade {grade} on the Kokoro model card; the best "
                     f"US voices are {', '.join(RECOMMENDED_VOICES)} (STUDIO_TTS_VOICE)")
        durations, words = _kokoro_tts(segs, audio, voice, speed)
    else:
        raise StageError(f"tts: unknown engine {engine!r} (kokoro, mock, silence)")

    t = 0.0
    for seg, dur in zip(segs, durations, strict=True):
        seg["start"], seg["end"] = round(t, 3), round(t + dur, 3)
        t += dur + GAP_S
    total = media_duration(audio)
    segs[-1]["end"] = round(total, 3)
    timings.write_text(
        json.dumps({"engine": engine, "duration": total, "segments": segs}, indent=2)
    )
    outputs = [audio, timings]
    if words is not None:
        words_path.write_text(json.dumps(words, indent=1))
        outputs.append(words_path)
    else:
        words_path.unlink(missing_ok=True)  # never leave a stale file from another engine
    job.audio_path = audio
    write_cache(job, STAGE, key, outputs)
    log(job, f"tts: {engine} voice={voice} speed={speed} {total:.1f}s")
    return job
