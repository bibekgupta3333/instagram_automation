import re
from pathlib import Path

import pytest
import yaml

from studio.config import (
    DEFAULT_BRAND_DIR,
    BrandPackError,
    load_brand,
    load_brand_file,
    load_brands,
)
from studio.contracts import BrandPack, Presenter, Script
from studio.pipeline.mock_llm import MockChatClient
from studio.stages.policy import lint_script

SLUGS = {
    "square-foot-money", "software-shortcut", "business-model-files", "car-cost-truth",
    "gravity-what-if", "megabuild-blueprint", "lock-screen-logic", "furnace-to-faucet",
    "wing-and-turbine", "rank-test-lab",
}
# Kids packs (made_for_kids=True) are local-only pilots; publishing stays blocked by the gate.
KIDS_SLUGS = {"plinkety-pond"}
ACCURACY_CRITICAL = {"square-foot-money", "lock-screen-logic", "furnace-to-faucet", "wing-and-turbine"}
KOKORO_US = re.compile(r"^a[fm]_[a-z]+$")


@pytest.fixture(scope="module")
def brands() -> dict[str, BrandPack]:
    return load_brands(DEFAULT_BRAND_DIR)


def test_all_ten_packs_exist_and_validate(brands):
    assert set(brands) == SLUGS | KIDS_SLUGS
    for path in DEFAULT_BRAND_DIR.glob("*.yaml"):
        BrandPack.model_validate(yaml.safe_load(path.read_text()))


@pytest.mark.parametrize("slug", sorted(SLUGS))
def test_pack_fields(brands, slug):
    b = brands[slug]
    assert b.name and b.niche and b.style_prompt
    assert b.handle == "@" + b.slug.replace("-", "")
    assert len(b.palette) == 3 and all(re.fullmatch(r"#[0-9A-F]{6}", c) for c in b.palette)
    assert b.voice.engine == "kokoro" and KOKORO_US.match(b.voice.voice)
    assert b.platforms and b.hook_styles and b.banned_templates and b.red_lines
    assert b.accuracy_critical == (slug in ACCURACY_CRITICAL)
    assert b.made_for_kids is False and b.ai_disclosure is True


def test_voices_are_distinct(brands):
    voices = [b.voice.voice for b in brands.values()]
    assert len(set(voices)) == len(voices)


def test_all_channels_faceless(brands):
    # HeyGen avatars removed 2026-10-16 (doc 12 §2): every channel is faceless, no avatar host.
    # Presenter.AVATAR stays in the contract for possible future use.
    assert all(b.presenter == Presenter.FACELESS for b in brands.values())
    assert all(b.avatar_name is None for b in brands.values())


def test_unknown_key_and_slug_mismatch_rejected(tmp_path: Path):
    data = yaml.safe_load((DEFAULT_BRAND_DIR / "gravity-what-if.yaml").read_text())
    bad = tmp_path / "gravity-what-if.yaml"
    bad.write_text(yaml.safe_dump({**data, "colour": "red"}))
    with pytest.raises(BrandPackError, match="unknown keys"):
        load_brand_file(bad)
    other = tmp_path / "other.yaml"
    other.write_text(yaml.safe_dump(data))
    with pytest.raises(BrandPackError, match="must match"):
        load_brand_file(other)
    with pytest.raises(BrandPackError, match="unknown channel"):
        load_brand("nope", DEFAULT_BRAND_DIR)


@pytest.mark.parametrize("slug", sorted(SLUGS))
def test_mock_script_is_lint_clean_for_every_brand(brands, slug):
    script = Script.model_validate(MockChatClient().script("What if the Moon disappeared?"))
    assert lint_script(script, brands[slug]) == []


@pytest.mark.parametrize("slug", sorted(KIDS_SLUGS))
def test_kids_pack_fields(brands, slug):
    b = brands[slug]
    assert b.made_for_kids is True and b.ai_disclosure is True
    assert b.presenter == Presenter.FACELESS and not b.avatar_name
    assert b.red_lines and b.banned_templates and "no text" in b.style_prompt
