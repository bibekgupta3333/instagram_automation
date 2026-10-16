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
        """Deterministic per job + beat, in 1..4294967294 (0 means random for Stability)."""
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
    usd: float = 0.0
    source: dict[str, Any] = field(default_factory=dict)  # attribution for stock

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Visual:
        return cls(**{**d, "path": Path(d["path"])})


def record_cost(*, channel: str, job_id: str, step: str, model: str, usd: float,
                outcome: str = "ok") -> bool:
    """Record a paid media call in the spend ledger if ``studio.llm.budget`` provides one.

    Prefers a module-level ``record_spend``/``record_media_spend`` function; otherwise writes a
    row through ``Ledger`` (route=bedrock). Returns False (and only logs) when unavailable, so
    a missing ledger never fails a render. Set STUDIO_SKIP_LEDGER=1 to disable.
    """
    if os.environ.get("STUDIO_SKIP_LEDGER") == "1":
        return False
    try:
        from studio.llm import budget  # lazy: owned by the LLM layer
    except ImportError:
        logger.info("no studio.llm.budget; not recording $%.3f for %s", usd, model)
        return False
    for name in ("record_media_spend", "record_spend"):
        fn = getattr(budget, name, None)
        if callable(fn):
            try:
                fn(channel=channel, job_id=job_id, step=step, route="bedrock", model=model,
                   usd=usd, outcome=outcome)
                return True
            except TypeError:
                break
    ledger_cls = getattr(budget, "Ledger", None)
    if ledger_cls is None:
        return False
    try:
        ledger = ledger_cls()
        try:
            ledger.record(channel=channel, job_id=job_id, step=step, route="bedrock",
                          model=model, usage=None, usd=usd, outcome=outcome)
        finally:
            close = getattr(ledger, "close", None)
            if callable(close):
                close()
        return True
    except Exception as exc:  # noqa: BLE001 - the ledger must never break the stage
        logger.warning("ledger write failed for %s ($%.3f): %s", model, usd, exc)
        return False
