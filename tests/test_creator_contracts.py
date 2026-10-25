"""General creator contracts must not weaken the separate nursery schema."""
import pytest
from pydantic import ValidationError

from studio.web.creator_models import VideoBrief
from studio.web.models import Project, storyboard


def test_general_creator_platforms_and_optional_cast():
    brief = VideoBrief(idea="A quiet train crossing a glowing forest", style="watercolor-anime")
    assert brief.characters == []
    assert brief.audience == "general"
    assert brief.platforms == ["youtube", "instagram", "facebook"]
    assert VideoBrief(idea=brief.idea, audio="narration").audio == "narration"


@pytest.mark.parametrize("changes", [
    {"audience": "kids"}, {"platforms": ["youtube", "youtube"]},
    {"duration": 12}, {"duration": 65}, {"duration": 0},
    {"style": "unknown"}, {"endpoint": "http://169.254.169.254"},
    {"characters": [{"name": "Mira", "description": ""}]},
])
def test_invalid_or_unsupported_briefs_rejected(changes):
    with pytest.raises(ValidationError):
        VideoBrief(idea="An original little adventure", **changes)


def test_kids_classification_stays_explicit_and_nursery_unchanged():
    assert VideoBrief(idea="Count original toy boats", audience="kids",
                      platforms=["youtube"]).audience == "kids"
    characters, scenes = storyboard("bubbles")
    with pytest.raises(ValidationError):
        Project(characters=characters, scenes=scenes, made_for_kids=False)
