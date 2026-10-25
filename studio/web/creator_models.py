"""General short-video contracts; separate from the nursery editor's strict schema."""
from typing import Literal

from pydantic import Field, model_validator

from .models import ID, Model, Name, now, uid

Style = Literal["watercolor-anime", "anime", "cartoon", "cinematic", "live-action", "motion-graphics"]
Platform = Literal["youtube", "instagram", "facebook"]


class CastMember(Model):
    name: Name
    description: str = Field(min_length=3, max_length=1200)


class VideoBrief(Model):
    idea: str = Field(min_length=5, max_length=4000)
    style: Style = "cinematic"
    audience: Literal["general", "kids"] = "general"
    platforms: list[Platform] = Field(default_factory=lambda: ["youtube", "instagram", "facebook"],
                                      min_length=1, max_length=3)
    duration: int = Field(default=15, ge=5, le=60, multiple_of=5)
    characters: list[CastMember] = Field(default_factory=list, max_length=4)
    language: Literal["en"] = "en"
    audio: Literal["ambient", "silent", "narration"] = "ambient"
    title: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def audience_targets(self):
        if len(set(self.platforms)) != len(self.platforms):
            raise ValueError("Choose each platform only once")
        if self.audience == "kids" and self.platforms != ["youtube"]:
            raise ValueError("Kids projects use YouTube only")
        return self


class VideoShot(Model):
    number: int = Field(ge=1, le=12)
    duration: Literal[5] = 5
    prompt: str = Field(min_length=5, max_length=2400)
    caption: str = Field(default="", max_length=120)
    narration: str = Field(default="", max_length=500)


class VideoPlan(Model):
    title: Name
    hook: str = Field(min_length=1, max_length=500)
    characters: list[CastMember] = Field(default_factory=list, max_length=4)
    shots: list[VideoShot] = Field(min_length=1, max_length=12)
    description: str = Field(default="", max_length=2000)
    hashtags: list[str] = Field(default_factory=list, max_length=12)
    planner: Literal["opencode", "template"] = "template"


class Creation(Model):
    id: ID = Field(default_factory=uid)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    revision: int = Field(default=1, ge=1)
    brief: VideoBrief
    plan: VideoPlan | None = None


class CreationRender(Model):
    mode: Literal["preview", "cloud"] = "preview"
    max_cost_usd: float = Field(default=0, ge=0, le=20)


class SaveCreation(Model):
    revision: int = Field(ge=1)
    brief: VideoBrief
    plan: VideoPlan | None = None
