"""Shared plumbing for pipeline stages: context, errors, cache keys, hashing and ffmpeg helpers.

Every stage is `run(job, brand, ctx) -> job`. A stage is idempotent: it writes
`<workdir>/.cache/<stage>.json` holding the hash of its inputs, and skips when that
key is unchanged and all its outputs still exist.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from studio.config import Settings
from studio.contracts import ChatClient, VideoJob

DEFAULT_ENGINES = {"tts": "kokoro", "captions": "parakeet", "images": "z-image-turbo"}
MOCK_ENGINES = {"tts": "mock", "captions": "mock", "images": "mock"}


class StageError(RuntimeError):
    """A stage failed in a way that should mark the job FAILED (not a crash)."""


class EngineUnavailable(StageError):
    """A real engine was requested but its package or model is not installed."""


@dataclass
class StageContext:
    settings: Settings
    chat: ChatClient
    engines: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ENGINES))
    force: bool = False  # ignore cache keys and re-run every stage
    trace: list[tuple[str, str]] = field(default_factory=list)  # (stage, job_id) run order

    def engine(self, stage: str) -> str:
        return self.engines.get(stage, DEFAULT_ENGINES.get(stage, "mock"))


# ---------------------------------------------------------------- hashing / cache keys


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_inputs(*parts: Any) -> str:
    """Stable hash of JSON-able values; Paths are hashed by file content."""

    def norm(v: Any) -> Any:
        if isinstance(v, Path):
            return {"file": v.name, "sha256": sha256_file(v) if v.exists() else None}
        if isinstance(v, dict):
            return {str(k): norm(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        if hasattr(v, "model_dump"):
            return v.model_dump(mode="json")
        return v

    blob = json.dumps([norm(p) for p in parts], sort_keys=True, default=str)
    return sha256_bytes(blob.encode())


def cache_file(job: VideoJob, stage: str) -> Path:
    return Path(job.workdir) / ".cache" / f"{stage}.json"


def is_cached(job: VideoJob, stage: str, key: str, outputs: Iterable[Path], ctx: StageContext) -> bool:
    if ctx.force:
        return False
    path = cache_file(job, stage)
    if not path.exists():
        return False
    try:
        stored = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    return stored.get("key") == key and all(Path(p).exists() for p in outputs)


def write_cache(job: VideoJob, stage: str, key: str, outputs: Iterable[Path], **extra: Any) -> None:
    path = cache_file(job, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stage": stage,
        "key": key,
        "outputs": [str(p) for p in outputs],
        "written_at": datetime.now().isoformat(timespec="seconds"),
        **extra,
    }
    path.write_text(json.dumps(payload, indent=2))


def log(job: VideoJob, msg: str) -> None:
    job.log.append(f"{datetime.now().isoformat(timespec='seconds')} {msg}")


# ---------------------------------------------------------------- ffmpeg helpers


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise StageError(f"{name} not found on PATH (brew install ffmpeg)")
    return path


def run_ffmpeg(args: list[str], cwd: Path | None = None) -> None:
    cmd = [require_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, check=False, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise StageError(f"ffmpeg failed ({proc.returncode}):\n{tail}")


def ffprobe(path: Path) -> dict[str, Any]:
    cmd = [
        require_binary("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise StageError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def media_duration(path: Path) -> float:
    return float(ffprobe(path)["format"]["duration"])


# ---------------------------------------------------------------- colour helpers


def hex_to_rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def luminance(hex_colour: str) -> float:
    r, g, b = (c / 255 for c in hex_to_rgb(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def saturation(hex_colour: str) -> float:
    rgb = [c / 255 for c in hex_to_rgb(hex_colour)]
    mx, mn = max(rgb), min(rgb)
    return 0.0 if mx == 0 else (mx - mn) / mx


def palette_roles(palette: list[str]) -> dict[str, str]:
    """Pick background (darkest), accent (most saturated mid-tone) and light colours."""
    pal = palette or ["#111111", "#FFC400", "#FFFFFF"]
    dark = min(pal, key=luminance)
    light = max(pal, key=luminance)
    rest = [c for c in pal if c not in (dark,)] or pal
    accent = max(rest, key=lambda c: saturation(c) * (0.4 + luminance(c)))
    return {"dark": dark, "accent": accent, "light": light}
