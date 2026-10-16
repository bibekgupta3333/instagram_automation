import pytest

from studio.contracts import Platform, PublishRequest
from studio.publish.gate import PublishBlocked, assert_publishable, url_problems


def req(**over):
    base = {
        "job_id": "j1",
        "channel": "c01",
        "platforms": [Platform.YT_SHORTS, Platform.IG_REELS, Platform.FB_REELS],
        "media_url": "https://cdn.example.com/c01/j1.mp4",
        "thumbnail_url": "https://cdn.example.com/c01/j1.jpg",
        "title": "Why your attic fan matters",
        "caption_by_platform": {
            "youtube_shorts": "desc #a",
            "instagram_reels": "cap #a #b",
            "facebook_reels": "fb cap",
        },
        "content_hash": "abc",
        "approved_hash": "abc",
    }
    base.update(over)
    return PublishRequest(**base)


def blocked(r, match):
    with pytest.raises(PublishBlocked) as ei:
        assert_publishable(r)
    assert any(match in e for e in ei.value.errors), ei.value.errors
    assert isinstance(ei.value, PermissionError)


def test_valid_request_passes():
    assert_publishable(req())


def test_rejects_unapproved():
    blocked(req(approved_hash="other"), "content changed after approval")
    blocked(req(approved_hash=""), "no recorded human approval")


@pytest.mark.parametrize(
    "url",
    [
        "https://bucket.s3.amazonaws.com/v.mp4?X-Amz-Algorithm=AWS4&X-Amz-Signature=deadbeef",
        "https://d1.cloudfront.net/v.mp4?Expires=1700000000&Signature=abc&Key-Pair-Id=K",
        "https://storage.googleapis.com/b/v.mp4?X-Goog-Signature=abc",
        "https://acct.blob.core.windows.net/c/v.mp4?se=2026-10-20&sig=abc",
    ],
)
def test_rejects_presigned(url):
    blocked(req(media_url=url), "pre-signed")


@pytest.mark.parametrize(
    "url,match",
    [
        ("http://cdn.example.com/v.mp4", "https"),
        ("https://localhost/v.mp4", "not public"),
        ("https://10.0.0.5/v.mp4", "not a public address"),
        ("https://drive.google.com/file/d/x", "permanent public CDN"),
    ],
)
def test_rejects_non_public_urls(url, match):
    blocked(req(media_url=url), match)


def test_presigned_thumbnail_also_rejected():
    blocked(req(thumbnail_url="https://x.s3.amazonaws.com/t.jpg?X-Amz-Signature=1"), "thumbnail")


def test_rejects_made_for_kids_and_missing_disclosure():
    blocked(req(made_for_kids=True), "made_for_kids")
    blocked(req(ai_disclosure=False), "ai_disclosure")


def test_caption_limits():
    blocked(
        req(
            caption_by_platform={
                "youtube_shorts": "d",
                "facebook_reels": "f",
                "instagram_reels": "x" * 2201,
            }
        ),
        "max 2200",
    )
    blocked(
        req(
            caption_by_platform={
                "youtube_shorts": "d",
                "facebook_reels": "f",
                "instagram_reels": "#a #b #c #d #e #f",
            }
        ),
        "max 5",
    )
    blocked(req(title="t" * 101), "max 100")
    blocked(req(caption_by_platform={"youtube_shorts": "d"}), "no caption for instagram_reels")


def test_url_ok():
    assert url_problems("https://cdn.example.com/a.mp4?v=2", "m") == []
