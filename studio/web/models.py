"""Strict wire models for doc 30; independent of the publishing pipeline."""
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
Text = Annotated[str, Field(max_length=4000)]
Name = Annotated[str, Field(min_length=1, max_length=120)]


def uid() -> str:
    return uuid4().hex


def now() -> str:
    return datetime.now(UTC).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Character(Model):
    id: ID = Field(default_factory=uid)
    name: Name
    species: Literal["otter", "rabbit", "bear", "frog"] = "otter"
    color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] = "#A66A42"
    accessory: Literal["scarf", "bow", "none"] = "none"
    description: Text = "Original pond friend"
    asset_id: ID | None = None


class Scene(Model):
    id: ID = Field(default_factory=uid)
    title: Name
    duration: float = Field(default=5, ge=1, le=15)
    narration: Text = ""
    caption: Annotated[str, Field(max_length=500)] = ""
    background: Literal["pond", "meadow", "bedtime", "studio"] = "pond"
    motion: Literal["bounce", "sway", "float"] = "sway"
    character_ids: list[ID] = Field(default_factory=list, max_length=8)
    camera: Literal["wide", "close"] = "wide"


class Audio(Model):
    asset_id: ID | None = None
    mode: Literal["guide", "uploaded"] = "guide"
    volume: float = Field(default=0.7, ge=0, le=1)


class Review(Model):
    originality: bool = False
    rights: bool = False
    child_safety: bool = False
    lyrics: bool = False
    visuals: bool = False


class Project(Model):
    id: ID = Field(default_factory=uid)
    name: Name = "Untitled pond story"
    topic: Annotated[str, Field(max_length=500)] = "Bubbles at the pond"
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    revision: int = Field(default=1, ge=1)
    made_for_kids: Literal[True] = True
    ai_disclosure: Literal[True] = True
    characters: list[Character] = Field(min_length=1, max_length=8)
    scenes: list[Scene] = Field(min_length=1, max_length=24)
    audio: Audio = Field(default_factory=Audio)
    review: Review = Field(default_factory=Review)
    format: Literal["landscape", "portrait"] = "landscape"
    resolution: Literal["draft", "final"] = "draft"

    @model_validator(mode="after")
    def validate_story(self):
        ids = [c.id for c in self.characters]
        scene_ids = [s.id for s in self.scenes]
        if len(ids) != len(set(ids)) or len(scene_ids) != len(set(scene_ids)):
            raise ValueError("Character and scene IDs must be unique")
        if sum(s.duration for s in self.scenes) > 120:
            raise ValueError("Total duration exceeds 120 seconds")
        if any(set(s.character_ids) - set(ids) for s in self.scenes):
            raise ValueError("Scene references an unknown character")
        if self.audio.mode == "uploaded" and not self.audio.asset_id:
            raise ValueError("Uploaded audio needs an asset")
        return self


class CreateProject(Model):
    name: Name = "Untitled pond story"
    topic: Annotated[str, Field(max_length=500)] = "Bubbles at the pond"
    template: Literal["bubbles", "bedtime", "counting"] = "bubbles"


class Plan(Model):
    topic: Annotated[str, Field(min_length=1, max_length=500)]
    template: Literal["bubbles", "bedtime", "counting"]


class JobRequest(Model):
    kind: Literal["render", "character"]
    character_id: ID | None = None
    provider: Literal["illustration", "mflux", "diffusers"] = "illustration"
    format: Literal["landscape", "portrait"] | None = None
    resolution: Literal["draft", "final"] | None = None


class Session(Model):
    token: Annotated[str, Field(max_length=4096)]


def storyboard(template: str) -> tuple[list[Character], list[Scene]]:
    """Original otter drafts, explicitly template-based rather than LLM output."""
    pebble = Character(name="Pebble", accessory="scarf", description="Original playful otter")
    fern = Character(name="Fern", color="#BD8256", accessory="bow",
                     description="Original thoughtful otter who notices little pond discoveries")
    mum = Character(name="Mum", color="#80543F", accessory="none",
                    description="Original gentle parent otter with a warm chestnut coat")
    bloop = Character(name="Bloop", color="#D8AA77", accessory="scarf",
                      description="Original curious little otter with a golden sandy coat")
    lines = {
        "bubbles": [("Hello bubbles", "Pebble spots a bubble shining by the pond."),
                    ("A gentle float", "Fern watches it float above a lily leaf."),
                    ("Wave goodbye", "Together they wave as the bubble drifts away.")],
        "bedtime": [("Evening pond", "The pond grows quiet under a soft blue sky."),
                    ("Cosy friends", "Pebble and Fern curl up beside their cosy den."),
                    ("Good night", "The otters close their eyes as stars begin to shine.")],
        "counting": [("One leaf", "One little leaf floats past Pebble."),
                     ("Two ripples", "Fern watches two round ripples spread."),
                     ("Three waves", "The friends give three gentle waves goodbye.")],
    }
    scenes = [Scene(title=title, narration=line, caption=title,
                    background="bedtime" if template == "bedtime" else "pond",
                    character_ids=[pebble.id, fern.id]) for title, line in lines[template]]
    return [pebble, fern, mum, bloop], scenes
