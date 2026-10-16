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
import os
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
VERSION = 2
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

_kokoro: Any = None  # resident KokoroPipeline (holds the model), one per process / batch stage


def check_voice(voice: str) -> None:
    unknown = [v for v in voice.split(",") if v.strip() not in KOKORO_US_VOICES]
    if unknown and os.environ.get("STUDIO_KOKORO_ANY_VOICE") != "1":
        raise StageError(f"tts: unknown Kokoro US voice(s) {unknown}; choose from "
                         f"{sorted(KOKORO_US_VOICES)} (STUDIO_KOKORO_ANY_VOICE=1 skips this)")


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


def _load_kokoro() -> Any:
    """Load Kokoro once per process (or per batch stage) and return its English pipeline.

    Verified against mlx-audio 0.5.9: `mlx_audio.tts.utils.load(repo)` returns the Kokoro
    `Model` (sample_rate 24000). Its `generate()` yields audio only, so we drive
    `KokoroPipeline` directly: it yields `Result(graphemes, phonemes, tokens, output)` where
    each misaki token carries `start_ts`/`end_ts` (seconds, from the predicted durations
    that produced the audio) and `whitespace`. G2P is misaki + spacy en_core_web_sm.
    """
    global _kokoro
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
            pipeline = KokoroPipeline(lang_code="a", model=model, repo_id=KOKORO_MODEL)
        except ImportError as exc:  # misaki / spacy model missing
            raise EngineUnavailable(f"TTS engine 'kokoro': G2P not installed ({exc}). "
                                    "Run `uv sync --extra local-audio`.") from exc
        _kokoro = pipeline
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
    pipeline = _load_kokoro()  # raises EngineUnavailable first if mlx-audio is missing
    import numpy as np  # installed with mlx-audio

    rate = int(getattr(pipeline.model, "sample_rate", SAMPLE_RATE))
    gap = np.zeros(int(GAP_S * rate), dtype=np.float32)
    pieces, durations, words = [], [], []
    t = 0.0
    for i, seg in enumerate(segs):
        try:
            results = list(pipeline(seg["text"], voice=voice, speed=speed))
        except (FileNotFoundError, ValueError, RuntimeError) as exc:  # bad voice id, mostly
            raise StageError(f"tts: kokoro failed for voice {voice!r} ({KOKORO_MODEL}): {exc}") \
                from exc
        chunks = [np.asarray(r.audio, dtype=np.float32).reshape(-1)
                  for r in results if r.audio is not None]
        audio = np.concatenate(chunks) if chunks else np.zeros(rate // 2, dtype=np.float32)
        for w in _segment_words(results, rate):
            words.append({**w, "start": round(t + w["start"], 3), "end": round(t + w["end"], 3)})
        # MLX keeps freed Metal buffers in its cache; with varying segment lengths that grew
        # to ~15 GB over one 35 s script (measured). Clearing per segment holds it at ~3.5 GB.
        _clear_mlx_cache()
        dur = len(audio) / rate
        durations.append(dur)
        pieces.append(audio)
        t += dur
        if i < len(segs) - 1:
            pieces.append(gap)
            t += len(gap) / rate
    pcm = (np.clip(np.concatenate(pieces), -1, 1) * 32767).astype("<i2")
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return durations, words


def _clear_mlx_cache() -> None:
    try:
        import mlx.core as mx
    except ImportError:
        return
    mx.clear_cache()


def unload() -> None:
    """Free the resident TTS model (run_batch calls this after the TTS stage)."""
    global _kokoro
    _kokoro = None
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
    key = hash_inputs(STAGE, VERSION, engine, brand.voice, [s["text"] for s in segs])
    expected = [audio, timings] + ([words_path] if engine == "kokoro" else [])
    if is_cached(job, STAGE, key, expected, ctx):
        job.audio_path = audio
        log(job, "tts: cached, skipped")
        return job

    speed = brand.voice.speed
    words: list[dict[str, Any]] | None = None
    if engine in ("mock", "silence"):
        durations = _mock(segs, audio, speed, tone=engine == "mock")
    elif engine == "kokoro":
        durations, words = _kokoro_tts(segs, audio, brand.voice.voice, speed)
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
    log(job, f"tts: {engine} voice={brand.voice.voice} {total:.1f}s")
    return job
