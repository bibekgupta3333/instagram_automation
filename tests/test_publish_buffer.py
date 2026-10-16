import json
from datetime import UTC, datetime

import httpx
import pytest

from studio.contracts import Platform, PublishRequest
from studio.publish.buffer import (
    BufferPublisher,
    NeedsReconcile,
    PostLedger,
    RateLimited,
    build_create_post_input,
    idempotency_key,
)
from studio.publish.gate import PublishBlocked


def req(**over):
    base = {
        "job_id": "j1",
        "channel": "c01",
        "platforms": [Platform.IG_REELS, Platform.YT_SHORTS],
        "media_url": "https://cdn.example.com/c01/j1.mp4",
        "thumbnail_url": "https://cdn.example.com/c01/j1.jpg",
        "title": "Attic fans, explained",
        "caption_by_platform": {"instagram_reels": "cap #fan", "youtube_shorts": "desc"},
        "scheduled_at": datetime(2026, 10, 20, 13, 30, tzinfo=UTC),
        "content_hash": "h",
        "approved_hash": "h",
    }
    base.update(over)
    return PublishRequest(**base)


CHANNELS = {"c01:instagram_reels": "IG1", "c01:youtube_shorts": "YT1", "c01:facebook_reels": "FB1"}


def success(req_):
    body = json.loads(req_.content)
    cid = body["variables"]["input"]["channelId"]
    return httpx.Response(
        200,
        json={
            "data": {
                "createPost": {
                    "__typename": "PostActionSuccess",
                    "post": {"id": f"post-{cid}", "dueAt": "x"},
                }
            }
        },
    )


def pub(tmp_path, handler, **kw):
    calls = []

    def wrapped(r):
        calls.append(r)
        return handler(r)

    p = BufferPublisher(
        api_key="k",
        channel_map=CHANNELS,
        ledger=PostLedger(tmp_path / "l.db"),
        transport=httpx.MockTransport(wrapped),
        sleep=lambda s: None,
        **kw,
    )
    return p, calls


def test_dry_run_is_default_and_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("BUFFER_API_KEY", raising=False)
    p, calls = pub(tmp_path, lambda r: pytest.fail("network call in dry run"), second_gate="none")
    p.api_key = ""
    assert p.dry_run is True
    results = p.publish(req(), first_comments={"instagram_reels": "Link in bio"})
    assert calls == [] and [r.status for r in results] == ["dry_run", "dry_run"]
    ig = results[0].payload["variables"]["input"]
    assert ig == {
        "channelId": "IG1",
        "text": "cap #fan",
        "schedulingType": "automatic",
        "assets": [
            {
                "video": {
                    "url": "https://cdn.example.com/c01/j1.mp4",
                    "thumbnailUrl": "https://cdn.example.com/c01/j1.jpg",
                }
            }
        ],
        "needsApproval": False,
        "mode": "customScheduled",
        "dueAt": "2026-10-20T13:30:00.000Z",
        "metadata": {
            "instagram": {
                "type": "reel",
                "shouldShareToFeed": True,
                "isAiGenerated": True,
                "firstComment": "Link in bio",
            }
        },
    }
    yt = results[1].payload["variables"]["input"]
    assert yt["metadata"]["youtube"] == {
        "title": "Attic fans, explained",
        "privacy": "public",
        "madeForKids": False,
        "isAiGenerated": True,
        "notifySubscribers": True,
    }
    assert "thumbnailUrl" not in yt["assets"][0]["video"]  # no custom Shorts thumbnail
    assert "createPost(input: $input)" in results[0].payload["query"]


def test_second_gate_options():
    r = req()
    draft = build_create_post_input(r, Platform.IG_REELS, channel_id="x", second_gate="draft")
    assert draft["saveToDraft"] is True and draft["needsApproval"] is False
    appr = build_create_post_input(r, Platform.IG_REELS, channel_id="x", second_gate="approval")
    assert appr["needsApproval"] is True and "saveToDraft" not in appr
    fb = req(scheduled_at=None, caption_by_platform={"facebook_reels": "fb"})
    q = build_create_post_input(fb, Platform.FB_REELS, channel_id="x")
    assert q["mode"] == "addToQueue" and q["metadata"] == {"facebook": {"title": r.title}}


def test_gate_runs_before_anything(tmp_path):
    p, _calls = pub(tmp_path, success, dry_run=True)
    with pytest.raises(PublishBlocked):
        p.publish(req(approved_hash="nope"))


def test_idempotency_prevents_second_post(tmp_path):
    p, calls = pub(tmp_path, success, dry_run=False, second_gate="none")
    first = p.publish(req())
    assert [r.status for r in first] == ["posted", "posted"] and len(calls) == 2
    again = p.publish(req())
    assert [r.status for r in again] == ["skipped_duplicate", "skipped_duplicate"]
    assert [r.remote_id for r in again] == ["post-IG1", "post-YT1"]
    assert len(calls) == 2
    # a fresh publisher on the same SQLite ledger also refuses to re-post
    p2, calls2 = pub(tmp_path, success, dry_run=False)
    assert p2.publish(req())[0].status == "skipped_duplicate" and calls2 == []


def test_unknown_outcome_requires_reconcile(tmp_path):
    p, calls = pub(tmp_path, lambda r: httpx.Response(502), dry_run=False)
    with pytest.raises(httpx.HTTPStatusError):
        p.publish(req(platforms=[Platform.IG_REELS]))
    with pytest.raises(NeedsReconcile):
        p.publish(req(platforms=[Platform.IG_REELS]))
    assert len(calls) == 1
    led = p.ledger.get(idempotency_key("j1", "instagram_reels"))
    assert led["status"] == "pending"


def test_mutation_error_allows_retry(tmp_path):
    answers = iter(
        [
            httpx.Response(
                200,
                json={
                    "data": {"createPost": {"__typename": "MutationError", "message": "bad media"}}
                },
            ),
        ]
    )
    p, _calls = pub(tmp_path, lambda r: next(answers, None) or success(r), dry_run=False)
    with pytest.raises(Exception, match="bad media"):
        p.publish(req(platforms=[Platform.IG_REELS]))
    assert p.publish(req(platforms=[Platform.IG_REELS]))[0].status == "posted"


def test_rate_limit_window(tmp_path):
    t = [1_000_000.0]
    p, calls = pub(tmp_path, success, dry_run=False, clock=lambda: t[0], window_limit=2)
    p.publish(req())  # 2 requests
    with pytest.raises(RateLimited) as ei:
        p.publish(req(job_id="j2"))
    assert 0 < ei.value.retry_after <= 900 and len(calls) == 2
    t[0] += 901
    assert p.publish(req(job_id="j2"))[0].status == "posted"


def test_http_429_retried(tmp_path):
    seq = iter([httpx.Response(429, headers={"Retry-After": "1"})])
    p, calls = pub(tmp_path, lambda r: next(seq, None) or success(r), dry_run=False)
    assert p.publish(req(platforms=[Platform.IG_REELS]))[0].status == "posted"
    assert len(calls) == 2


def test_long_form_not_sent_to_buffer(tmp_path):
    p, calls = pub(tmp_path, success, dry_run=False)
    r = req(platforms=[Platform.YT_LONG], caption_by_platform={"youtube_long": "d"})
    assert p.publish(r) == [] and calls == []


def test_live_mode_requires_key(tmp_path, monkeypatch):
    monkeypatch.delenv("BUFFER_API_KEY", raising=False)
    with pytest.raises(Exception, match="BUFFER_API_KEY"):
        BufferPublisher(dry_run=False, ledger=PostLedger(tmp_path / "l.db"), channel_map={})
