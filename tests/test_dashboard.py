"""Dashboard tests: offline, loopback, read-only (doc 37 §8)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from studio.dashboard.server import make_server
from studio.pipeline import pipeline_schema


def _serve(db, out):
    srv = make_server("127.0.0.1", 0, db, out)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def _j(url):
    return json.loads(_get(url)[1])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("STUDIO_DAILY_CAP", raising=False)
    monkeypatch.setenv("STUDIO_LEDGER_PATH", str(tmp_path / "none.sqlite"))
    out = tmp_path / "output"
    (out / "j1").mkdir(parents=True)
    (out / "j1" / "final.mp4").write_bytes(b"0123456789")
    (out / "j1" / "job.json").write_text(json.dumps({"script": {"title": "T"}}))
    (tmp_path / "secret.txt").write_text("secret")
    db = out / "pipeline.sqlite"
    con = pipeline_schema.connect(db)
    now = time.time()
    con.execute("INSERT INTO batches(name,status) VALUES('b1','done')")
    con.execute("INSERT INTO trend_candidates(term,source,fetched_at,day) VALUES('t','rss',?, '2026-10-29')", (now,))
    con.execute("INSERT INTO topics(topic_text,topic_hash,class,status,reject_reason,created_at) "
                "VALUES('x','h1','evergreen','rejected','unsafe',?)", (now,))
    for jid, ok, valid, draft in [("j1", 1, 1, 0), ("j2", 1, None, 0), ("j3", 0, 0, 1)]:
        con.execute("INSERT INTO jobs(job_id,batch_id,channel,format,topic,status,workdir,video_path,approval_valid,"
                    "is_draft,created_at,updated_at) VALUES(?,1,'chan','short','topic',?,?,?,?,?,?,?)",
                    (jid, "done", str(out / jid), str(out / "j1" / "final.mp4"), valid, draft, now, now))
        for g in ("policy", "fact_check"):
            con.execute("INSERT INTO gate_results(job_id,gate,passed,issues_json,created_at) VALUES(?,?,?,?,?)",
                        (jid, g, ok, json.dumps(["bad claim"]) if not ok else None, now))
    con.execute("INSERT INTO stage_runs(job_id,stage,status,seconds) VALUES('j1','script','ok',2)")
    con.execute("INSERT INTO assets(job_id,beat,source,creator,licence) VALUES('j1',1,'pixabay','me','pixabay')")
    con.execute("INSERT INTO publishes(job_id,platform,target,status,updated_at) VALUES('j3','youtube','buffer','sent',?)", (now,))
    con.commit()
    con.close()
    srv, base = _serve(db, out)
    yield base, tmp_path
    srv.shutdown()


def test_endpoints(env):
    base, _ = env
    d = _j(base + "/api/jobs")
    assert not d["empty"] and len(d["jobs"]) == 3
    assert len(_j(base + "/api/jobs?draft=1")["jobs"]) == 1
    j = _j(base + "/api/job?id=j1")
    assert j["approve_hint"] == "studio approve j1 --by <your-name>"
    assert j["job"]["approval"] == "valid" and j["script"] == {"title": "T"}
    assert [s["status"] for s in j["stages"]][:2] == ["ok", "pending"] and j["video_url"]
    assert _j(base + "/api/topics")["reject_reasons"][0]["n"] == 1
    g = _j(base + "/api/gates")
    assert g["failures"] and any(r["rate"] == 0.333 for r in g["rates"])
    p = _j(base + "/api/publish")
    assert [r["job_id"] for r in p["approved_not_published"]] == ["j1"]
    assert [r["job_id"] for r in p["published_without_valid_approval"]] == ["j3"]
    c = _j(base + "/api/capacity")
    assert c["cap"] == 20 and c["today_count"] == 3 and c["warning"] is None and c["spend"] is None
    assert [r["name"] for r in _j(base + "/api/batches")["batches"]] == ["b1"]
    assert [r["job_id"] for r in _j(base + "/api/review")["jobs"]] == ["j2"]
    assert "guarded_running" in _j(base + "/api/live")
    assert b"Studio dashboard" in _get(base + "/")[1]


def test_cap_warning(env, monkeypatch):
    monkeypatch.setenv("STUDIO_DAILY_CAP", "150")
    c = _j(env[0] + "/api/capacity")
    assert "100" in c["warning"] and "Facebook 30" in c["warning"]


def test_media_traversal(env):
    base, tmp = env
    ok = _get(base + "/media?path=" + str(tmp / "output" / "j1" / "final.mp4"))
    assert ok[0] == 200 and ok[1] == b"0123456789"
    for bad in [str(tmp / "secret.txt"), str(tmp / "output" / ".." / "secret.txt"),
                "../secret.txt", "/etc/passwd", "%2e%2e%2fsecret.txt"]:
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(base + "/media?path=" + bad)
        assert e.value.code == 404


def test_symlink_escape(env):
    base, tmp = env
    (tmp / "output" / "j1" / "link.txt").symlink_to(tmp / "secret.txt")
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base + "/media?path=" + str(tmp / "output" / "j1" / "link.txt"))
    assert e.value.code == 404


def test_read_only_methods(env):
    base, _ = env
    for m in ("POST", "PUT", "DELETE", "PATCH"):
        req = urllib.request.Request(base + "/api/jobs", data=b"{}", method=m)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 405
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base + "/api/approve")
    assert e.value.code == 404


def test_bad_host_header(env):
    base, _ = env
    req = urllib.request.Request(base + "/api/jobs", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", ""])
def test_non_loopback_refused(host):
    with pytest.raises(ValueError):
        make_server(host, 0)


def test_empty_db(tmp_path):
    srv, base = _serve(tmp_path / "missing.sqlite", tmp_path / "output")
    try:
        for ep in ("jobs", "topics", "gates", "publish", "capacity", "batches", "review"):
            d = _j(f"{base}/api/{ep}")
            assert d["empty"] and "studio track sync" in d["hint"]
        assert not (tmp_path / "missing.sqlite").exists()  # never created by the dashboard
    finally:
        srv.shutdown()


def test_missing_tables(tmp_path):
    import sqlite3
    db = tmp_path / "p.sqlite"
    sqlite3.connect(db).close()
    srv, base = _serve(db, tmp_path)
    try:
        assert _j(base + "/api/gates")["empty"]
    finally:
        srv.shutdown()
