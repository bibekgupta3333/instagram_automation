"""Offline tests for studio.pipeline.tracking (doc 37)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from studio.cli import app
from studio.pipeline import pipeline_schema as schema
from studio.pipeline.tracking import (
    record_gate_result,
    record_job_event,
    sync_from_jobs,
)


def _job(out: Path, jid: str, *, draft=False, **over) -> Path:
    d = out / ("drafts" if draft else "") / jid
    d.mkdir(parents=True)
    data = {
        "job_id": jid, "channel": "chan", "format": "short", "topic": "t", "status": "rendered",
        "script": {"x": 1}, "fact_check": {"passed": True, "issues": [], "model": "m"},
        "workdir": str(d), "audio_path": "a.wav", "captions_path": "c.ass",
        "image_paths": ["i.png"], "video_path": str(d / "v.mp4"), "content_hash": "h" + jid,
        "approved_hash": None, "approved_by": None, "created_at": "2026-10-29T00:00:00",
        "log": [],
    }
    data.update(over)
    (d / "job.json").write_text(json.dumps(data))
    if draft:
        (d / "DRAFT_NOT_VERIFIED.txt").write_text("x")
    return d


def _rows(db, sql):
    con = schema.connect(db, readonly=True)
    try:
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


def test_sync_idempotent_and_draft(tmp_path):
    out, db = tmp_path / "output", tmp_path / "p.sqlite"
    d = _job(out, "j1")
    (d / ".cache").mkdir()
    (d / ".cache" / "script.json").write_text(
        json.dumps({"stage": "script", "written_at": "2026-10-29T01:00:00"}))
    _job(out, "dr", draft=True, fact_check=None)
    c1 = sync_from_jobs(out, db)
    snap = _rows(db, "SELECT job_id,status,current_stage,is_draft FROM jobs ORDER BY job_id")
    c2 = sync_from_jobs(out, db)
    assert c1 == c2
    assert snap == _rows(db, "SELECT job_id,status,current_stage,is_draft FROM jobs "
                             "ORDER BY job_id")
    assert {r["job_id"]: r["is_draft"] for r in snap} == {"j1": 0, "dr": 1}
    st = {r["stage"]: r["status"] for r in _rows(db, "SELECT * FROM stage_runs WHERE job_id='dr'")}
    assert st["fact_check"] == "skipped"
    st = {r["stage"]: r["status"] for r in _rows(db, "SELECT * FROM stage_runs WHERE job_id='j1'")}
    assert st["script"] == "ok" and st["render"] == "cached"
    assert _rows(db, "SELECT current_stage FROM jobs WHERE job_id='j1'")[0]["current_stage"] is None


def test_failed_job_and_gates(tmp_path):
    out, db = tmp_path / "output", tmp_path / "p.sqlite"
    d = _job(out, "bad", status="failed", audio_path=None, captions_path=None, image_paths=[],
             video_path=None, fact_check={"passed": False, "issues": ["misleading: x"]},
             log=["t fact_check: FAILED: fact_check failed: misleading: x"])
    (d / "script_quality.json").write_text(json.dumps([{"passed": True, "issues": []}]))
    sync_from_jobs(out, db)
    j = _rows(db, "SELECT * FROM jobs")[0]
    assert j["status"] == "failed" and j["current_stage"] == "fact_check"
    assert "misleading" in j["error"]
    sr = {r["stage"]: r["status"] for r in _rows(db, "SELECT * FROM stage_runs")}
    assert sr["fact_check"] == "failed" and "tts" not in sr
    g = {r["gate"]: r["passed"] for r in _rows(db, "SELECT * FROM gate_results")}
    assert g == {"fact_check": 0, "policy": 1}


def test_assets_visuals_and_attribution_fallback(tmp_path):
    out, db = tmp_path / "output", tmp_path / "p.sqlite"
    d = _job(out, "a1")
    (d / "images").mkdir()
    (d / "images" / "visuals.json").write_text(json.dumps({"beats": [{"beat": 0, "visual": {
        "path": "p.mp4", "kind": "video", "engine": "pixabay", "source": {
            "provider": "Pixabay", "license": "PCL", "id": 7, "creator": "bob",
            "source_url": "https://pixabay.com/videos/id-7/"}}}]}))
    d2 = _job(out, "a2")
    (d2 / "images").mkdir()
    (d2 / "images" / "attribution.txt").write_text(
        "Video by Kane on Pixabay (id 9): https://pixabay.com/videos/id-9/\n")
    sync_from_jobs(out, db)
    a = {r["job_id"]: r for r in _rows(db, "SELECT * FROM assets")}
    assert a["a1"]["creator"] == "bob" and a["a1"]["licence"] == "PCL" and a["a1"]["source_id"] == "7"
    assert a["a2"]["creator"] == "Kane" and a["a2"]["source"] == "Pixabay"


def test_publishes_mapping_and_missing_tables(tmp_path):
    out, db = tmp_path / "output", tmp_path / "p.sqlite"
    _job(out, "p1")
    led = out / "ledger.sqlite"
    sqlite3.connect(led).close()  # no publish tables at all
    assert sync_from_jobs(out, db)["publishes"] == 0
    con = sqlite3.connect(led)
    con.execute("CREATE TABLE publish_posts (idem_key TEXT, job_id TEXT, platform TEXT, "
                "content_hash TEXT, status TEXT, remote_id TEXT, error TEXT, updated_at TEXT)")
    con.execute("INSERT INTO publish_posts VALUES ('k','p1','instagram','hp1','pending',"
                "'DRY_RUN:k',NULL,'2026-10-29T00:00:00')")
    con.execute("INSERT INTO publish_posts VALUES ('k2','ghost','instagram','x','posted','r',"
                "NULL,'2026-10-29T00:00:00')")
    con.execute("CREATE TABLE youtube_uploads (content_hash TEXT, video_sha256 TEXT, "
                "channel TEXT, status TEXT, video_id TEXT, updated_at TEXT)")
    con.execute("INSERT INTO youtube_uploads VALUES ('hp1','s','chan','uploaded','vid',"
                "'2026-10-29T00:00:00')")
    con.commit()
    con.close()
    assert sync_from_jobs(out, db)["publishes"] == 2
    p = {r["target"]: r for r in _rows(db, "SELECT * FROM publishes")}
    assert p["buffer_dry_run"]["status"] == "pending"
    assert p["youtube_private"]["remote_id"] == "vid" and p["youtube_private"]["status"] == "posted"


def test_approval_mirrored_never_written(tmp_path):
    out, db = tmp_path / "output", tmp_path / "p.sqlite"
    d = _job(out, "ap", status="approved", approved_hash="hap", approved_by="me")
    before = (d / "job.json").read_text()
    sync_from_jobs(out, db)
    j = _rows(db, "SELECT * FROM jobs")[0]
    assert j["approved_by"] == "me" and j["approval_valid"] in (0, None)
    assert (d / "job.json").read_text() == before


def test_event_helpers_never_raise(tmp_path):
    out, db = tmp_path / "output", tmp_path / "p.sqlite"
    _job(out, "e1")
    sync_from_jobs(out, db)
    assert record_job_event("e1", stage="tts", status="ok", seconds=1.5, db=db)
    assert not record_job_event("nope", stage="tts", status="ok", db=db)
    assert record_gate_result("e1", "publish_gate", False, ["x"], db=db)
    assert record_job_event("e1", db=tmp_path / "no" / "\0bad") is False


def test_cli_smoke(tmp_path, monkeypatch):
    out, db = tmp_path / "output", tmp_path / "p.sqlite"
    _job(out, "c1", status="failed", log=["t script: FAILED: boom"])
    _job(out, "cd", draft=True)
    monkeypatch.setenv("STUDIO_PIPELINE_DB", str(db))
    r = CliRunner()
    res = r.invoke(app, ["track", "sync", "--output-dir", str(out)])
    assert res.exit_code == 0, res.output
    res = r.invoke(app, ["track", "report", "--json"])
    assert res.exit_code == 0, res.output
    rep = json.loads(res.output)
    assert rep["failed"][0]["job_id"] == "c1" and rep["drafts"][0]["job_id"] == "cd"
    assert r.invoke(app, ["track", "report"]).exit_code == 0
