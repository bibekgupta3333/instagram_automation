import pytest

from studio.contracts import Platform, PublishRequest
from studio.publish import youtube


def req():
    return PublishRequest(
        job_id="j",
        channel="c",
        platforms=[Platform.YT_LONG],
        media_url="https://cdn.example.com/v.mp4",
        title="T",
        caption_by_platform={"youtube_long": "desc"},
        tags=["a"],
        content_hash="h",
        approved_hash="h",
    )


def test_insert_body_forces_private_until_audited(monkeypatch):
    monkeypatch.delenv("YOUTUBE_PROJECT_AUDITED", raising=False)
    body = youtube.build_insert_body(req(), privacy="public")
    assert body["status"] == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": True,
        "embeddable": True,
    }
    assert (
        youtube.build_insert_body(req(), privacy="public", audited=True)["status"]["privacyStatus"]
        == "public"
    )


def test_upload_without_google_libs(monkeypatch, tmp_path):
    monkeypatch.setattr(youtube, "google_libs_available", lambda: False)
    with pytest.raises(NotImplementedError, match="private"):
        youtube.upload(req(), tmp_path / "v.mp4")
