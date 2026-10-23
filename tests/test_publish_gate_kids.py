"""The reviewed kids path in the publish gate (user decision 2026-10-23, doc 26)."""

import pytest

from studio.config import load_brand
from studio.contracts import Platform, PublishRequest
from studio.publish.gate import PublishBlocked, assert_publishable, publish_problems

KIDS = load_brand("plinkety-pond")
ADULT = load_brand("car-cost-truth")


def req(channel="plinkety-pond", platforms=(Platform.YT_SHORTS,), **over):
    base = {
        "job_id": "k1",
        "channel": channel,
        "platforms": list(platforms),
        "media_url": "file:///tmp/video.mp4",
        "title": "Bubbles on My Tummy",
        "caption_by_platform": {p.value: "desc" for p in platforms},
        "made_for_kids": True,
        "content_hash": "abc",
        "approved_hash": "abc",
    }
    base.update(over)
    return PublishRequest(**base)


def blocked(r, match, **kw):
    with pytest.raises(PublishBlocked) as ei:
        assert_publishable(r, **kw)
    assert any(match in e for e in ei.value.errors), ei.value.errors


def test_kids_youtube_private_allowed():
    assert_publishable(req(), brand=KIDS, direct_privacy="private")
    assert_publishable(req(platforms=(Platform.YT_LONG,)), brand=KIDS, direct_privacy="private")


@pytest.mark.parametrize("p", [Platform.IG_REELS, Platform.FB_REELS])
def test_kids_instagram_facebook_blocked(p):
    blocked(req(platforms=(Platform.YT_SHORTS, p)), "only go to YouTube",
            brand=KIDS, direct_privacy="private")


@pytest.mark.parametrize("privacy", ["public", "unlisted", None])
def test_kids_non_private_blocked(privacy):
    blocked(req(), "private direct YouTube upload", brand=KIDS, direct_privacy=privacy)


def test_kids_missing_or_stale_approval_blocked():
    blocked(req(approved_hash=""), "no recorded human approval", brand=KIDS,
            direct_privacy="private")
    blocked(req(approved_hash="old"), "content changed after approval", brand=KIDS,
            direct_privacy="private")


def test_kids_requires_ai_disclosure():
    blocked(req(ai_disclosure=False), "ai_disclosure", brand=KIDS, direct_privacy="private")


def test_adult_brand_cannot_declare_mfk():
    r = req(channel=ADULT.slug)
    blocked(r, "made_for_kids must be False", brand=ADULT, direct_privacy="private")
    blocked(r, "made_for_kids must be False")  # no brand passed: blocked as before


def test_kids_brand_cannot_be_mislabelled_not_for_kids():
    blocked(req(made_for_kids=False), "must declare made_for_kids=True", brand=KIDS,
            direct_privacy="private")


def test_brand_must_match_channel():
    blocked(req(channel="other"), "does not match", brand=KIDS, direct_privacy="private")


def test_adult_direct_private_allowed_and_public_blocked():
    r = req(channel=ADULT.slug, made_for_kids=False)
    assert_publishable(r, brand=ADULT, direct_privacy="private")
    blocked(r, "must be private", brand=ADULT, direct_privacy="public")


def test_buffer_path_unchanged_still_checks_urls():
    r = req(channel=ADULT.slug, made_for_kids=False)
    assert any("https" in e for e in publish_problems(r))  # file:// is not a Buffer URL
    blocked(req(), "made_for_kids")  # no brand, no direct upload
