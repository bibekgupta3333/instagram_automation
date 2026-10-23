"""Offline tests for the private-only YouTube uploader (no googleapiclient, no network)."""

import stat
from datetime import UTC, datetime, timedelta

import pytest

from studio.config import load_brand
from studio.contracts import Platform, PublishRequest
from studio.publish import youtube
from studio.publish.gate import PublishBlocked

KIDS = load_brand("plinkety-pond")


def req(**over):
    base = {
        "job_id": "j",
        "channel": "plinkety-pond",
        "platforms": [Platform.YT_SHORTS],
        "media_url": "file:///tmp/v.mp4",
        "title": "Bubbles on My Tummy",
        "caption_by_platform": {"youtube_shorts": "desc"},
        "tags": ["bath song"],
        "made_for_kids": True,
        "content_hash": "h",
        "approved_hash": "h",
    }
    base.update(over)
    return PublishRequest(**base)


def meta(**over):
    base = {"title": "Bubbles on My Tummy", "description": "A bath song.",
            "tags": ["bath song", "nursery rhyme"], "made_for_kids": True}
    base.update(over)
    return youtube.UploadMeta(**base)


# ---------------------------------------------------------------- body


def test_body_is_private_with_mfk_and_synthetic_flags():
    body = youtube.build_video_body(meta())
    assert body["status"] == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": True,
        "containsSyntheticMedia": True,
        "embeddable": True,
    }
    sn = body["snippet"]
    assert sn["categoryId"] == "24"
    assert sn["defaultLanguage"] == sn["defaultAudioLanguage"] == "en-GB"
    assert sn["tags"] == ["bath song", "nursery rhyme"]
    adult = youtube.build_video_body(meta(made_for_kids=False))
    assert adult["status"]["selfDeclaredMadeForKids"] is False


@pytest.mark.parametrize("privacy", ["public", "unlisted", "PRIVATE", ""])
def test_any_other_privacy_rejected(privacy):
    with pytest.raises(youtube.PrivacyViolation):
        youtube.build_video_body(meta(privacy=privacy))
    with pytest.raises(youtube.PrivacyViolation):
        youtube.build_insert_body(req(), privacy=privacy)


def test_insert_body_adapter_private():
    body = youtube.build_insert_body(req())
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["selfDeclaredMadeForKids"] is True
    assert body["snippet"]["description"] == "desc"


def test_tags_length_counts_commas_and_quotes():
    assert youtube.tags_length([]) == 0
    assert youtube.tags_length(["ab", "c d"]) == 2 + 1 + (3 + 2)


@pytest.mark.parametrize(
    "over,match",
    [
        ({"title": "t" * 101}, "max 100"),
        ({"title": "  "}, "title is empty"),
        ({"description": "é" * 2501}, "bytes"),
        ({"title": "a <b>"}, "'<' or '>'"),
        ({"tags": ["x" * 250, "y" * 250]}, "max 500"),
        ({"tags": ["a,b"]}, "tags must"),
        ({"category_id": "22"}, "categoryId"),
        ({"contains_synthetic_media": False}, "containsSyntheticMedia"),
        ({"publish_at": datetime(2026, 1, 1)}, "timezone"),
        ({"publish_at": datetime(2020, 1, 1, tzinfo=UTC)}, "past"),
    ],
)
def test_meta_problems(over, match):
    errs = youtube.meta_problems(meta(**over))
    assert any(match in e for e in errs), errs
    with pytest.raises(youtube.MetaInvalid):
        youtube.build_video_body(meta(**over))


def test_schedule_sets_publish_at_and_stays_private():
    when = datetime.now(UTC) + timedelta(days=2)
    st = youtube.build_video_body(meta(publish_at=when))["status"]
    assert st["privacyStatus"] == "private"
    assert st["publishAt"].endswith("Z")
    assert "publishAt" not in youtube.build_video_body(meta())["status"]


def test_thumbnail_validation(tmp_path):
    bad = tmp_path / "t.gif"
    bad.write_bytes(b"x")
    assert any(".jpg or .png" in e for e in youtube.meta_problems(meta(thumbnail=bad)))
    assert any("not found" in e for e in youtube.meta_problems(meta(thumbnail=tmp_path / "n.png")))


def test_request_plan_lists_calls(tmp_path):
    th = tmp_path / "t.png"
    th.write_bytes(b"png")
    plan = youtube.request_plan(tmp_path / "v.mp4", meta(thumbnail=th, playlist_id="PL1"))
    assert [c["method"] for c in plan] == ["videos.insert", "thumbnails.set",
                                           "playlistItems.insert"]
    assert plan[0]["body"]["status"]["privacyStatus"] == "private"
    assert plan[2]["body"]["snippet"]["playlistId"] == "PL1"


def test_scopes():
    assert youtube.scopes_for(False) == [youtube.SCOPE_UPLOAD]
    assert youtube.SCOPE_YOUTUBE in youtube.scopes_for(True)


def test_token_written_0600(tmp_path):
    p = tmp_path / "cfg" / "tok.json"
    youtube._write_token(p, "{}")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


# ---------------------------------------------------------------- upload with a fake service


class FakeHttpError(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.resp = type("R", (), {"status": status})()


class Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeInsert:
    def __init__(self, svc):
        self.svc = svc

    def next_chunk(self):
        if self.svc.insert_error:
            raise self.svc.insert_error
        return None, {"id": "vid123", "status": {"privacyStatus": self.svc.reported}}


class FakeService:
    def __init__(self, reported="private", insert_error=None, thumb_error=None):
        self.reported = reported
        self.insert_error = insert_error
        self.thumb_error = thumb_error
        self.calls = []

    def videos(self):
        svc = self

        class V:
            def insert(self, **kw):
                svc.calls.append(("videos.insert", kw))
                return FakeInsert(svc)

        return V()

    def thumbnails(self):
        svc = self

        class T:
            def set(self, **kw):
                svc.calls.append(("thumbnails.set", kw))

                def run():
                    if svc.thumb_error:
                        raise svc.thumb_error
                    return {}

                return Exec(run)

        return T()

    def playlistItems(self):
        svc = self

        class P:
            def insert(self, **kw):
                svc.calls.append(("playlistItems.insert", kw))
                return Exec(lambda: {"id": "pli"})

        return P()

    def methods(self):
        return [c[0] for c in self.calls]


def media(path, mimetype, resumable):
    return ("media", str(path), mimetype, resumable)


@pytest.fixture
def files(tmp_path):
    v = tmp_path / "video.mp4"
    v.write_bytes(b"fake mp4")
    t = tmp_path / "thumb.png"
    t.write_bytes(b"png")
    return v, t


def do_upload(files, svc, ledger, **meta_over):
    v, t = files
    return youtube.upload(v, meta(thumbnail=t, **meta_over), request=req(), brand=KIDS,
                          service=svc, ledger=ledger, media_factory=media)


def test_upload_private_thumbnail_playlist(files):
    svc, ledger = FakeService(), youtube.YouTubeLedger(":memory:")
    res = do_upload(files, svc, ledger, playlist_id="PL1")
    assert res.video_id == "vid123" and not res.reused
    assert res.thumbnail_set and res.playlist_added
    assert svc.methods() == ["videos.insert", "thumbnails.set", "playlistItems.insert"]
    kw = svc.calls[0][1]
    assert kw["part"] == "snippet,status"
    assert kw["body"]["status"]["privacyStatus"] == "private"
    assert kw["body"]["status"]["selfDeclaredMadeForKids"] is True
    assert kw["media_body"][3] is True  # resumable
    assert ledger.get("h")["status"] == "uploaded"


def test_upload_is_idempotent_per_content_hash(files):
    svc, ledger = FakeService(), youtube.YouTubeLedger(":memory:")
    do_upload(files, svc, ledger)
    again = do_upload(files, svc, ledger)
    assert again.reused and again.video_id == "vid123"
    assert svc.methods().count("videos.insert") == 1
    assert svc.methods().count("thumbnails.set") == 1


def test_thumbnail_failure_retried_alone(files):
    svc, ledger = FakeService(thumb_error=RuntimeError("boom")), youtube.YouTubeLedger(":memory:")
    assert not do_upload(files, svc, ledger).thumbnail_set
    svc.thumb_error = None
    res = do_upload(files, svc, ledger)
    assert res.reused and res.thumbnail_set
    assert svc.methods().count("videos.insert") == 1


def test_interrupted_upload_needs_reconcile(files):
    svc, ledger = FakeService(insert_error=ConnectionError("reset")), youtube.YouTubeLedger(
        ":memory:")
    with pytest.raises(ConnectionError):
        do_upload(files, svc, ledger)
    assert ledger.get("h")["status"] == "pending"
    svc.insert_error = None
    with pytest.raises(youtube.NeedsReconcile):
        do_upload(files, svc, ledger)


def test_definitive_4xx_allows_retry(files):
    svc, ledger = FakeService(insert_error=FakeHttpError(400)), youtube.YouTubeLedger(":memory:")
    with pytest.raises(FakeHttpError):
        do_upload(files, svc, ledger)
    assert ledger.get("h")["status"] == "failed"
    svc.insert_error = None
    assert do_upload(files, svc, ledger).video_id == "vid123"


def test_non_private_response_raises(files):
    with pytest.raises(youtube.PrivacyViolation, match="not private"):
        do_upload(files, FakeService(reported="public"), youtube.YouTubeLedger(":memory:"))


def test_upload_runs_gate_first(files):
    v, _ = files
    svc = FakeService()
    with pytest.raises(PublishBlocked, match="human approval"):
        youtube.upload(v, meta(), request=req(approved_hash=""), brand=KIDS, service=svc,
                       ledger=youtube.YouTubeLedger(":memory:"), media_factory=media)
    with pytest.raises(ValueError, match="match the brand"):
        youtube.upload(v, meta(made_for_kids=False), request=req(), brand=KIDS, service=svc,
                       ledger=youtube.YouTubeLedger(":memory:"), media_factory=media)
    assert svc.calls == []


def test_upload_without_google_libs(monkeypatch, files):
    monkeypatch.setattr(youtube, "google_libs_available", lambda: False)
    v, _ = files
    with pytest.raises(NotImplementedError, match="private"):
        youtube.upload(v, meta(), request=req(), brand=KIDS,
                       ledger=youtube.YouTubeLedger(":memory:"))
