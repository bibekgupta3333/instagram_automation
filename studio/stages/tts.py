"""Stage 3: narration. Kokoro-82M via mlx-audio (doc 14 §4), or a mock engine built on
`ffmpeg -f lavfi` (sine tone or silence) whose length is estimated at 150 wpm.

Writes narration.wav plus timings.json (start/end of the hook, each beat and the CTA),
which the captions and render stages use to time words and images to the narration.
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
VERSION = 1
SAMPLE_RATE = 24_000
WPM = 150
GAP_S = 0.08  # "cut the air" (06 §3): only a breath between segments
KOKORO_MODEL = os.environ.get("STUDIO_KOKORO_MODEL", "mlx-community/Kokoro-82M-bf16")

_kokoro: Any = None  # resident model, loaded once per process / batch stage


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
    global _kokoro
    if _kokoro is None:
        try:
            from mlx_audio.tts.utils import load_model  # lazy: heavy, optional (local-ml extra)
        except ImportError as exc:
            raise EngineUnavailable(
                "TTS engine 'kokoro' needs mlx-audio, which is not installed. Install the local-ml "
                "extra (`uv sync --extra local-ml`) or run with --mock / --engine tts=mock."
            ) from exc
        _kokoro = load_model(KOKORO_MODEL)
    return _kokoro


def _kokoro_tts(segs: list[dict[str, Any]], out: Path, voice: str, speed: float) -> list[float]:
    model = _load_kokoro()  # raises EngineUnavailable first if mlx-audio is missing
    import numpy as np  # installed with mlx-audio

    rate = int(getattr(model, "sample_rate", SAMPLE_RATE))
    gap = np.zeros(int(GAP_S * rate), dtype=np.float32)
    pieces, durations = [], []
    for i, seg in enumerate(segs):
        chunks = [
            np.asarray(r.audio, dtype=np.float32).reshape(-1)
            for r in model.generate(text=seg["text"], voice=voice, speed=speed, lang_code="a")
        ]
        audio = np.concatenate(chunks) if chunks else np.zeros(rate // 2, dtype=np.float32)
        durations.append(len(audio) / rate)
        pieces.append(audio)
        if i < len(segs) - 1:
            pieces.append(gap)
    pcm = (np.clip(np.concatenate(pieces), -1, 1) * 32767).astype("<i2")
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return durations


def unload() -> None:
    """Free the resident TTS model (called by run_batch after the TTS stage)."""
    global _kokoro
    _kokoro = None
    gc.collect()


# ---------------------------------------------------------------- stage


def run(job: VideoJob, brand: BrandPack, ctx: StageContext) -> VideoJob:
    if job.script is None:
        raise StageError("tts: no script")
    workdir = Path(job.workdir)
    audio, timings = workdir / "narration.wav", workdir / "timings.json"
    engine = ctx.engine("tts")
    segs = segments(job.script)
    key = hash_inputs(STAGE, VERSION, engine, brand.voice, [s["text"] for s in segs])
    if is_cached(job, STAGE, key, [audio, timings], ctx):
        job.audio_path = audio
        log(job, "tts: cached, skipped")
        return job

    speed = brand.voice.speed
    if engine in ("mock", "silence"):
        durations = _mock(segs, audio, speed, tone=engine == "mock")
    elif engine == "kokoro":
        durations = _kokoro_tts(segs, audio, brand.voice.voice, speed)
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
    job.audio_path = audio
    write_cache(job, STAGE, key, [audio, timings])
    log(job, f"tts: {engine} voice={brand.voice.voice} {total:.1f}s")
    return job
