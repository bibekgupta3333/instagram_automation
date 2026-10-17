"""Shared types and helpers for visual sources."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from studio.stages.base import sha256_bytes

logger = logging.getLogger("studio.visuals")

VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".webm"})


def configure_cpu_threads(*, cv2: Any = None, torch: Any = None) -> None:
    """Limit libraries the caller already uses; never import a runtime just to tune it."""
    try:
        threads = max(1, int(os.environ.get("STUDIO_CPU_THREADS", "3")))
    except ValueError:
        threads = 3
    if cv2 is not None:
        cv2.setNumThreads(threads)
    if torch is not None:
        torch.set_num_threads(threads)


def is_video(path: Path | str) -> bool:
    return Path(path).suffix.lower() in VIDEO_SUFFIXES


SEED_SPAN = 2**31 - 1024  # keeps base + beat offset inside a signed 32-bit seed


def base_seed(job_id: str) -> int:
    """One deterministic seed per video (job), in 1..SEED_SPAN."""
    return int(sha256_bytes(f"seed:{job_id}".encode())[:8], 16) % SEED_SPAN + 1


def beat_seed(job_id: str, index: int) -> int:
    """The per-video base seed plus the beat offset, so a video's stills share one noise
    family and a re-run reproduces every beat exactly."""
    return base_seed(job_id) + index


@dataclass(frozen=True)
class BeatSpec:
    """What a source needs to produce the visual for one beat."""

    job_id: str
    channel: str
    index: int
    fmt: str  # "short" (9:16) | "long" (16:9)
    visual_prompt: str  # the script's prompt, without the brand style
    prompt: str  # the full generation prompt (studio.stages.images.build_prompt)
    out_stem: Path  # images/beat_00 (the source picks the suffix)
    style_prompt: str = ""
    shot: str = ""  # Beat.shot, e.g. "wide establishing", "close-up detail", "aerial"
    stock_query: str = ""  # Beat.stock_query: 2-4 keywords for stock search (preferred)

    @property
    def seed(self) -> int:
        """Per-video base seed plus the beat offset (see base_seed)."""
        return beat_seed(self.job_id, self.index)

    @property
    def aspect_ratio(self) -> str:
        return "9:16" if self.fmt == "short" else "16:9"

    @property
    def orientation(self) -> str:
        return "portrait" if self.fmt == "short" else "landscape"


@dataclass
class Visual:
    """One produced visual. ``kind`` is "image" (still, zoompan) or "video" (clip)."""

    path: Path
    kind: str
    engine: str
    model: str
    usd: float = 0.0  # always 0 (no paid sources); kept so older visuals.json still loads
    source: dict[str, Any] = field(default_factory=dict)  # attribution for stock

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Visual:
        return cls(**{**d, "path": Path(d["path"])})
