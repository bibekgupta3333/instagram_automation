"""Shared types and helpers for visual sources."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from studio.stages.base import sha256_bytes

logger = logging.getLogger("studio.visuals")

VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".webm"})


def is_video(path: Path | str) -> bool:
    return Path(path).suffix.lower() in VIDEO_SUFFIXES


@dataclass(frozen=True)
class BeatSpec:
    """What a source needs to produce the visual for one beat."""

    job_id: str
    channel: str
    index: int
    fmt: str  # "short" (9:16) | "long" (16:9)
    visual_prompt: str  # the script's prompt, without the brand style
    prompt: str  # visual_prompt + brand style_prompt
    out_stem: Path  # images/beat_00 (the source picks the suffix)
    style_prompt: str = ""

    @property
    def seed(self) -> int:
        """Deterministic per job + beat, in 1..4294967294."""
        return int(sha256_bytes(f"{self.job_id}:{self.index}".encode())[:8], 16) % 4294967294 + 1

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
