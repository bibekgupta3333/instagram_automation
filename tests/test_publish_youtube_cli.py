"""Offline tests for scripts/youtube_upload.py (fake run dir, fake YouTube service)."""

import io
import json

import pytest

from scripts import youtube_upload as cli
from studio.publish import youtube
from tests.test_publish_youtube import FakeService


class TTY(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_ENV_FILE", "off")
    monkeypatch.setenv("STUDIO_LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setattr(youtube, "_media", lambda p, m, r: ("media", str(p), m, r))
    d = tmp_path / "ep01"
    d.mkdir()
    (d / "video.mp4").write_bytes(b"fake mp4 bytes")
    (d / "thumb.png").write_bytes(b"png")
    (d / "run.json").write_text(json.dumps({
        "slug": "ep01", "made_for_kids": True, "ai_disclosure": True, "approved": False,
        "published": False, "mock_clips": False, "do_not_publish": False,
        "videos": {"9x16": {"path": "output/kids/ep01/video.mp4"}},
    }))
    (d / "meta.json").write_text(json.dumps({
        "title": "Bubbles on My Tummy | Bath Song", "description": "A gentle bath-time song.",
        "tags": ["bath song", "nursery rhymes"], "thumbnail": "thumb.png", "category_id": "24",
    }))
    return d


def args(*argv):
    return cli.build_parser().parse_args(list(argv))


def approve(d, typed=None):
    t = cli.resolve_target(d, d / "meta.json", cli._brand("plinkety-pond"))
    out = io.StringIO()
    code = cli.cmd_approve(args("approve", str(d), "--by", "Tester"),
                           stdin=TTY((typed or t.content_hash[:8]) + "\n"), stdout=out)
    return code, out.getvalue()


def dry_run(d, *extra):
    out = io.StringIO()
    code = cli.cmd_upload(args("upload", str(d), "--brand", "plinkety-pond", "--meta",
                               str(d / "meta.json"), "--dry-run", *extra), stdout=out)
    return code, json.loads(out.getvalue())


def test_dry_run_blocked_without_approval(run_dir):
    code, out = dry_run(run_dir)
    assert code == cli.EXIT_BLOCKED and out["gate"] == "BLOCKED"
    assert any("human approval" in p for p in out["problems"])
    assert out["calls"] is None


def test_approve_refuses_non_interactive(run_dir):
    with pytest.raises(cli.CliError, match="interactive terminal"):
        cli.cmd_approve(args("approve", str(run_dir), "--by", "X"), stdin=io.StringIO("x\n"),
                        stdout=io.StringIO())


def test_approve_wrong_confirmation_records_nothing(run_dir):
    with pytest.raises(cli.CliError, match="did not match"):
        approve(run_dir, typed="nope")
    assert "approvals" not in json.loads((run_dir / "run.json").read_text())


def test_approve_then_dry_run_prints_exact_body(run_dir):
    code, text = approve(run_dir)
    assert code == 0 and "AI agents must never run" in text
    run = json.loads((run_dir / "run.json").read_text())
    assert run["approved"] is True and run["approvals"][0]["approved_by"] == "Tester"
    code, out = dry_run(run_dir, "--playlist", "PL9")
    assert code == 0 and out["gate"] == "OK"
    body = out["calls"][0]["body"]
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["selfDeclaredMadeForKids"] is True
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["snippet"]["defaultAudioLanguage"] == "en-GB"
    assert [c["method"] for c in out["calls"]] == ["videos.insert", "thumbnails.set",
                                                   "playlistItems.insert"]


def test_edit_after_approval_voids_it(run_dir):
    approve(run_dir)
    meta = json.loads((run_dir / "meta.json").read_text())
    meta["title"] = "Changed title"
    (run_dir / "meta.json").write_text(json.dumps(meta))
    assert dry_run(run_dir)[0] == cli.EXIT_BLOCKED
    meta["title"] = "Bubbles on My Tummy | Bath Song"
    (run_dir / "meta.json").write_text(json.dumps(meta))
    (run_dir / "video.mp4").write_bytes(b"re-rendered")
    assert dry_run(run_dir)[0] == cli.EXIT_BLOCKED


def test_mock_run_refused(run_dir):
    run = json.loads((run_dir / "run.json").read_text())
    run["do_not_publish"] = True
    (run_dir / "run.json").write_text(json.dumps(run))
    with pytest.raises(cli.CliError, match="do_not_publish"):
        dry_run(run_dir)


def test_upload_and_idempotent_rerun(run_dir, tmp_path):
    approve(run_dir)
    svc = FakeService()
    ledger = youtube.YouTubeLedger(tmp_path / "ledger.sqlite")
    a = args("upload", str(run_dir), "--brand", "plinkety-pond", "--meta",
             str(run_dir / "meta.json"))
    out = io.StringIO()
    assert cli.cmd_upload(a, service=svc, ledger=ledger, stdout=out) == 0
    assert "PRIVATE video vid123" in out.getvalue()
    run = json.loads((run_dir / "run.json").read_text())
    assert run["youtube"]["privacy"] == "private" and run["published"] is False
    out = io.StringIO()
    assert cli.cmd_upload(a, service=svc, ledger=ledger, stdout=out) == 0
    assert "already uploaded" in out.getvalue()
    assert svc.methods().count("videos.insert") == 1
    status = io.StringIO()
    cli.cmd_status(args("status", str(run_dir)), ledger=ledger, stdout=status)
    st = json.loads(status.getvalue())
    assert st["run"]["approved"] and st["run"]["ledger"]["video_id"] == "vid123"


def test_schedule_requires_timezone(run_dir):
    approve(run_dir)
    with pytest.raises(cli.CliError, match="timezone"):
        dry_run(run_dir, "--schedule", "2030-01-01T09:00:00")
    code, out = dry_run(run_dir, "--schedule", "2030-01-01T09:00:00Z")
    assert code == 0
    st = out["calls"][0]["body"]["status"]
    assert st["privacyStatus"] == "private" and st["publishAt"] == "2030-01-01T09:00:00Z"
