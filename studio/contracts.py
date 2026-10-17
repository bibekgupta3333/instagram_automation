"""Shared data contracts between pipeline stages, the LLM layer and publishers.

Pipeline code (studio/stages, studio/pipeline) and integration code (studio/llm,
studio/publish, studio/flows) are written independently against these types.
Change them only in coordination with both sides.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class Platform(StrEnum):
    YT_SHORTS = "youtube_shorts"
    YT_LONG = "youtube_long"
    IG_REELS = "instagram_reels"
    FB_REELS = "facebook_reels"


class Presenter(StrEnum):
    FACELESS = "faceless"
    AVATAR = "avatar"


class VoiceSpec(BaseModel):
    engine: Literal["kokoro", "mock"] = "kokoro"
    voice: str = "am_michael"  # Kokoro voice id; synthetic, never a cloned real person
    speed: float = 1.0


class BrandPack(BaseModel):
    """Per-channel config loaded from brand_packs/<slug>.yaml (docs: 04 §5.5, 13)."""

    slug: str
    name: str
    handle: str
    niche: str
    presenter: Presenter
    avatar_name: str | None = None
    palette: list[str] = Field(default_factory=list)  # hex colours
    voice: VoiceSpec = Field(default_factory=VoiceSpec)
    platforms: list[Platform]
    shorts_per_day: float = 1.0
    longform_per_week: float = 0.0
    hook_styles: list[str] = Field(default_factory=list)
    banned_templates: list[str] = Field(default_factory=list)
    red_lines: list[str] = Field(default_factory=list)  # policy guardrails (docs 13 §3, 17)
    accuracy_critical: bool = False  # routes fact-check to Bedrock (docs 15)
    ai_disclosure: bool = True
    made_for_kids: bool = False
    style_prompt: str = ""  # appended to image prompts


class Beat(BaseModel):
    """One narrated segment of a script, with the visual to show during it."""

    text: str
    visual_prompt: str
    shot: str = ""  # shot type, e.g. "wide establishing", "close-up detail", "cutaway", "aerial"
    overlay: str = ""  # short on-screen text (<= 6 words) drawn by ffmpeg, never in the image
    stock_query: str = ""  # optional 2-4 keyword search for stock footage (Pixabay)


class Script(BaseModel):
    title: str
    hook: str
    beats: list[Beat]
    cta: str = ""
    sources: list[str] = Field(default_factory=list)
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)

    def narration(self) -> str:
        parts = [self.hook, *(b.text for b in self.beats), self.cta]
        return " ".join(p for p in parts if p)


class FactCheck(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    model: str = ""


class JobStatus(StrEnum):
    PLANNED = "planned"
    RENDERED = "rendered"
    APPROVED = "approved"
    REJECTED = "rejected"
    SCHEDULED = "scheduled"
    FAILED = "failed"


class VideoJob(BaseModel):
    """State of one video moving through the pipeline. Persisted as output/<job_id>/job.json."""

    job_id: str
    channel: str
    format: Literal["short", "long"] = "short"
    topic: str
    status: JobStatus = JobStatus.PLANNED
    script: Script | None = None
    fact_check: FactCheck | None = None
    workdir: Path
    audio_path: Path | None = None
    captions_path: Path | None = None
    image_paths: list[Path] = Field(default_factory=list)
    video_path: Path | None = None
    content_hash: str | None = None  # sha256 of video bytes + caption; approval binds to it
    approved_hash: str | None = None
    approved_by: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    log: list[str] = Field(default_factory=list)


class PublishRequest(BaseModel):
    """Hand-off to studio/publish (docs 04 §5.6, 05 §4). media_url must be a PUBLIC,
    non-expiring URL (no pre-signed links): Buffer fetches it at publish time."""

    job_id: str
    channel: str
    platforms: list[Platform]
    media_url: str
    thumbnail_url: str | None = None
    title: str
    caption_by_platform: dict[str, str]
    tags: list[str] = Field(default_factory=list)
    scheduled_at: datetime | None = None
    ai_disclosure: bool = True
    made_for_kids: bool = False
    content_hash: str
    approved_hash: str


class ChatClient(Protocol):
    """Implemented in studio/llm. `role` picks the model via the routing policy (docs 15 §5):
    e.g. "draft", "metadata", "polish", "fact_check"."""

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
    ) -> str: ...
