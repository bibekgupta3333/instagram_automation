"""Dashboard write-surface tests: offline, loopback, fake subprocess runner (doc 37)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio.dashboard.actions import ActionManager
from studio.dashboard.server import make_server
from studio.pipeline import pipeline_schema


class FakeRunner:
    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []
        self.gate = threading.Event()
        self.gate.set()
        self.exit_code = 0
        self.terminated = 0

    def __call__(self, argv, env, log_path, cwd):
        self.calls.append((list(argv), dict(env)))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as f:
            f.write(b"line1\nline2\n")
        me = self

        class H:
            def wait(self):
                me.gate.wait(10)
                return me.exit_code

            def terminate(self):
                me.terminated += 1
                me.gate.set()

        return H()


def _job(con, out, jid, *, status="rendered", draft=0, valid=None, channel="chan", gates=(1, 1),
         marker=False, h="a" * 64):
    d = out / jid
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.json").write_text(json.dumps({"job_id": jid, "channel": channel, "status": status,
                                            "content_hash": h}))
    if marker:
        (d / "DRAFT_NOT_VERIFIED.txt").write_text("x")
    now = time.time()
    con.execute("INSERT INTO jobs(job_id,channel,format,topic,status,workdir,content_hash,"
                "approval_valid,is_draft,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (jid, channel, "short", "t", status, str(d), h, valid, draft, now, now))
    for g, ok in zip(("policy", "fact_check"), gates, strict=True):
        if ok is not None:
            con.execute("INSERT INTO gate_results(job_id,gate,passed,created_at) VALUES(?,?,?,?)",
                        (jid, g, ok, now))


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("STUDIO_DAILY_CAP", "STUDIO_PLAN_DIR"):
        monkeypatch.delenv(k, raising=False)
    out = tmp_path / "output"
    out.mkdir()
    db = out / "pipeline.sqlite"
    con = pipeline_schema.connect(db)
    _job(con, out, "good-1")
    _job(con, out, "good-2", h="b" * 64)
    _job(con, out, "good-3", h="c" * 64)
    _job(con, out, "good-4", h="d" * 64)
    _job(con, out, "kid-1", channel="kids")
    _job(con, out, "draft-1", draft=1)
    _job(con, out, "draft-2", marker=True)
    _job(con, out, "failed-1", status="failed")
    _job(con, out, "ungated-1", gates=(1, None))
    _job(con, out, "gatefail-1", gates=(1, 0))
    _job(con, out, "approved-1", valid=1, status="approved")
    con.commit()
    con.close()
    runner = FakeRunner()
    brands = {"chan": SimpleNamespace(made_for_kids=False), "other": SimpleNamespace(made_for_kids=False),
              "kids": SimpleNamespace(made_for_kids=True)}
    plans = tmp_path / "plans"
    plans.mkdir()
    mgr = ActionManager(db, out, repo=tmp_path, runner=runner, pressure_fn=lambda: "green",
                        brand_loader=lambda: brands, studio_bin="/x/.venv/bin/studio",
                        python="/x/.venv/bin/python", plan_dir=plans, env={"PATH": "/usr/bin",
                        "AWS_PROFILE": "employer", "BUFFER_API_KEY": "k", "DRY_RUN": "0",
                        "STUDIO_ALLOW_MODEL_DOWNLOAD": "1"})
    srv = make_server("127.0.0.1", 0, db, out, manager=mgr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield SimpleNamespace(base=base, mgr=mgr, runner=runner, db=db, out=out, plans=plans,
                          brands=brands, port=srv.server_address[1])
    runner.gate.set()
    mgr.wait_idle()
    srv.shutdown()


def post(e, path, body=None, *, token=True, headers=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
    h = {"Content-Type": ctype}
    if token is True:
        h["X-Dashboard-Token"] = e.mgr.token
    elif token:
        h["X-Dashboard-Token"] = token
    h.update(headers or {})
    req = urllib.request.Request(e.base + path, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as ex:
        return ex.code, json.loads(ex.read() or b"{}")


def get(e, path):
    with urllib.request.urlopen(e.base + path, timeout=10) as r:
        return json.loads(r.read())


def act(e, name, body=None, **kw):
    code, out = post(e, f"/api/actions/{name}", body, **kw)
    e.mgr.wait_idle()
    return code, out


def rows(e, sql):
    con = sqlite3.connect(e.db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


# ---------------------------------------------------------------- security
def test_session_token_and_missing_wrong_token(env):
    assert get(env, "/api/session")["token"] == env.mgr.token
    assert post(env, "/api/actions/track_sync", token=False)[0] == 403
    assert post(env, "/api/actions/track_sync", token="nope")[0] == 403
    assert env.runner.calls == []


def test_origin_and_content_type_and_body(env):
    assert post(env, "/api/actions/track_sync", headers={"Origin": "https://evil.example"})[0] == 403
    assert post(env, "/api/actions/track_sync", headers={"Origin": f"http://127.0.0.1:{env.port + 1}"})[0] == 403
    assert post(env, "/api/actions/track_sync", headers={"Origin": "null"})[0] == 403
    assert post(env, "/api/actions/track_sync", ctype="text/plain")[0] == 400
    assert post(env, "/api/actions/track_sync", raw=b"{nope")[0] == 400
    assert post(env, "/api/actions/track_sync", raw=b"[1]")[0] == 400
    assert post(env, "/api/actions/track_sync", raw=b" " * 70000)[0] == 413
    code, _ = act(env, "track_sync", headers={"Origin": f"http://localhost:{env.port}"})
    assert code == 202


def test_only_whitelisted_posts_and_methods(env):
    assert post(env, "/api/jobs")[0] == 405
    assert post(env, "/api/actions/rm_rf")[0] == 404
    for m in ("PUT", "DELETE", "PATCH"):
        req = urllib.request.Request(env.base + "/api/actions/track_sync", data=b"{}", method=m)
        with pytest.raises(urllib.error.HTTPError) as ex:
            urllib.request.urlopen(req, timeout=5)
        assert ex.value.code == 405


def test_bad_host_header(env):
    req = urllib.request.Request(env.base + "/api/actions/track_sync", data=b"{}", method="POST",
                                 headers={"Host": "evil.example", "Content-Type": "application/json",
                                          "X-Dashboard-Token": env.mgr.token})
    with pytest.raises(urllib.error.HTTPError) as ex:
        urllib.request.urlopen(req, timeout=5)
    assert ex.value.code == 403


# ---------------------------------------------------------------- one at a time / rows / env
def test_concurrent_second_action_409(env):
    env.runner.gate.clear()
    code, first = post(env, "/api/actions/track_sync")
    assert code == 202 and first["status"] == "running"
    code, second = post(env, "/api/actions/track_sync")
    assert code == 409 and second["running_id"] == first["id"]
    code, stop = post(env, "/api/actions/run_stop")
    assert code == 200 and stop["stopped_id"] == first["id"] and env.runner.terminated == 1
    env.mgr.wait_idle()
    assert get(env, f"/api/actions/{first['id']}")["status"] == "stopped"
    refused = [r for r in rows(env, "SELECT * FROM actions") if r["status"] == "refused"]
    assert refused and "busy" in refused[0]["refused_reason"]


def test_run_stop_nothing_running(env):
    code, out = post(env, "/api/actions/run_stop")
    assert code == 422 and out["status"] == "refused"


def test_actions_rows_log_and_env(env):
    code, out = act(env, "track_sync")
    assert code == 202
    row = get(env, f"/api/actions/{out['id']}")
    assert row["status"] == "ok" and row["exit_code"] == 0 and row["argv"] == [["/x/.venv/bin/studio", "track", "sync"]]
    assert row["log_path"].endswith(f"dashboard-logs/{out['id']}.log")
    assert get(env, f"/api/actions/{out['id']}/log?tail=1")["lines"] == ["line2"]
    assert any(a["id"] == out["id"] for a in get(env, "/api/actions")["actions"])
    _, child = env.runner.calls[0]
    assert child["DRY_RUN"] == "1"
    assert "STUDIO_ALLOW_MODEL_DOWNLOAD" not in child
    assert not any(k.startswith(("AWS_", "BUFFER_")) for k in child)
    env.runner.exit_code = 3
    _, bad = act(env, "track_sync")
    assert get(env, f"/api/actions/{bad['id']}")["status"] == "failed"


def test_dry_run_cannot_be_overridden_by_params(env):
    for body in ({"DRY_RUN": "0"}, {"dry_run": False}, {"env": {"DRY_RUN": "0"}}):
        assert act(env, "track_sync", body)[0] == 400
    code, _ = act(env, "topics_fetch", {"source": "trends", "mock": True})
    assert code == 202 and env.runner.calls[-1][1]["DRY_RUN"] == "1"
    assert env.mgr.child_env({"DRY_RUN": "0", "AWS_PROFILE": "x"})["DRY_RUN"] == "1"
    assert "AWS_PROFILE" not in env.mgr.child_env({"AWS_PROFILE": "x"})


def test_read_only_tables_untouched_and_writer_restricted(env):
    def snap():
        return {t: rows(env, f"SELECT * FROM {t}") for t in ("jobs", "gate_results", "topics", "publishes")}
    before = snap()
    act(env, "track_sync")
    act(env, "job_reject", {"job_id": "good-1", "approver": "Ann"})
    assert snap() == before
    for sql in ("UPDATE jobs SET status='x'", "DELETE FROM gate_results", "DROP TABLE jobs",
                "INSERT INTO publishes(job_id,platform,target) VALUES('good-1','x','buffer')"):
        with pytest.raises(sqlite3.DatabaseError):
            env.mgr._write(sql)
    assert snap() == before


# ---------------------------------------------------------------- argv builders
def test_topics_actions_argv(env):
    act(env, "topics_fetch", {"source": "trends", "region": "us", "mock": True})
    assert env.runner.calls[-1][0] == ["/x/.venv/bin/studio", "topics", "fetch", "--source", "trends",
                                       "--region", "US", "--mock"]
    f = env.out / "t.txt"
    f.write_text("a")
    act(env, "topics_fetch", {"source": "file", "path": "t.txt"})
    assert env.runner.calls[-1][0][-2:] == ["--file", str(f.resolve())]
    code, _ = act(env, "topics_triage", {"mock_llm": True, "limit": 5})
    assert code == 202 and env.runner.calls[-1][1]["STUDIO_LLM"] == "mock"
    assert act(env, "topics_triage", {"llm": "bedrock"})[0] == 400
    assert act(env, "topics_triage", {})[0] == 400
    act(env, "topics_triage", {"llm": "opencode"})
    assert env.runner.calls[-1][1]["STUDIO_LLM"] == "opencode"
    act(env, "topics_pick", {"daily_cap": 99999, "per_channel": 2})
    assert env.runner.calls[-1][0][-4:] == ["--per-channel", "2", "--daily-cap", "1000"]
    act(env, "topics_pick", {})
    assert env.runner.calls[-1][0][-1] == "20"


def test_path_traversal_and_invalid_ids_400(env, tmp_path):
    (tmp_path / "secret.txt").write_text("s")
    for p in ("../secret.txt", str(tmp_path / "secret.txt"), "/etc/passwd", "nope.txt"):
        assert act(env, "topics_fetch", {"source": "file", "path": p})[0] == 400
    (env.out / "link").symlink_to(tmp_path / "secret.txt")
    assert act(env, "topics_fetch", {"source": "file", "path": "link"})[0] == 400
    for bad in ("../x", "a/b", "-rf", "a b", "", None, 5, "x" * 200):
        assert act(env, "job_reject", {"job_id": bad, "approver": "Ann"})[0] == 400
    n = len(env.runner.calls)
    assert act(env, "job_approve", {"job_id": "../../etc", "confirm_prefix": "x", "approver": "Ann"})[0] == 400
    assert len(env.runner.calls) == n


# ---------------------------------------------------------------- plan_run
def _plan(e, entries, day="2026-10-29"):
    import yaml
    (e.plans / f"{day}.yaml").write_text(yaml.safe_dump(entries))


def test_plan_run_builds_guarded_argv(env):
    _plan(env, [{"channel": "chan", "topic": "A", "format": "short"},
                {"channel": "other", "topic": "B", "format": "short"},
                {"channel": "chan", "topic": "C", "format": "short"}])
    code, _ = act(env, "plan_run", {"date": "2026-10-29", "mock": True})
    assert code == 202
    argv, child = env.runner.calls[-1]
    assert argv[:3] == ["/x/.venv/bin/python", "-m", "studio.dashboard.plan_driver"] and child["DRY_RUN"] == "1"
    man = json.loads(Path(argv[4]).read_text())["invocations"]
    assert [m["channel"] for m in man] == ["chan", "other"]
    a = man[0]["argv"]
    assert a[1].endswith("scripts/run_guarded.py") and a[2:5] == ["--", "/x/.venv/bin/studio", "batch"]
    assert "--mock" in a and Path(a[a.index("--topics-file") + 1]).read_text() == "A\nC\n"


def test_plan_run_real_needs_llm_and_green_pressure(env):
    _plan(env, [{"channel": "chan", "topic": "A"}])
    assert act(env, "plan_run", {"date": "2026-10-29"})[0] == 400
    env.mgr.pressure_fn = lambda: "yellow"
    code, out = act(env, "plan_run", {"date": "2026-10-29", "llm": "opencode"})
    assert code == 422 and "pressure" in out["refused_reason"] and not env.runner.calls
    assert act(env, "plan_run", {"date": "2026-10-29", "mock": True})[0] == 202  # mock is fine
    env.mgr.pressure_fn = lambda: "unknown"
    assert act(env, "plan_run", {"date": "2026-10-29", "llm": "claude-code"})[0] == 422
    env.mgr.pressure_fn = lambda: "green"
    assert act(env, "plan_run", {"date": "2026-10-29", "llm": "bedrock"})[0] == 400
    code, _ = act(env, "plan_run", {"date": "2026-10-29", "llm": "opencode"})
    assert code == 202 and env.runner.calls[-1][1]["STUDIO_LLM"] == "opencode"


def test_plan_run_daily_cap_refusal(env, monkeypatch):
    _plan(env, [{"channel": "chan", "topic": f"T{i}"} for i in range(3)])
    # 11 jobs exist today; cap 12 -> 3 more is over, max_jobs=1 fits
    env.mgr._env["STUDIO_DAILY_CAP"] = "12"
    code, out = act(env, "plan_run", {"date": "2026-10-29", "mock": True})
    assert code == 422 and "daily cap 12" in out["refused_reason"]
    assert act(env, "plan_run", {"date": "2026-10-29", "mock": True, "max_jobs": 1})[0] == 202
    assert act(env, "plan_run", {"date": "2026-10-30", "mock": True})[0] == 400  # no plan
    _plan(env, [{"channel": "ghost", "topic": "x"}])
    assert act(env, "plan_run", {"date": "2026-10-29", "mock": True})[0] == 400


def test_plan_driver_builds_and_rejects_foreign_argv(tmp_path):
    from studio.dashboard import plan_driver
    inv = plan_driver.build_invocations([{"channel": "c", "topic": "t", "format": "long"}],
                                        python="py", studio="st", run_guarded="rg.py",
                                        topics_dir=tmp_path, tag="x", mock=False, llm="mock")
    assert inv[0]["argv"][-2:] == ["--engine", "llm=mock"] and "--format" in inv[0]["argv"]
    man = tmp_path / "m.json"
    man.write_text(json.dumps({"invocations": [{"argv": ["rm", "-rf", "/", "a", "b"]}]}))
    assert plan_driver.main(["--manifest", str(man)]) == 2


# ---------------------------------------------------------------- approvals
def test_approve_refusals(env):
    for jid, why in (("kid-1", "kids"), ("draft-1", "draft"), ("draft-2", "DRAFT"), ("failed-1", "status"),
                     ("ungated-1", "fact_check"), ("gatefail-1", "gate"), ("approved-1", "status"),
                     ("missing-1", "tracking DB")):
        code, out = act(env, "job_approve", {"job_id": jid, "confirm_prefix": "a" * 12, "approver": "Ann"})
        assert code == 422 and why in out["refused_reason"], (jid, out)
    assert not env.runner.calls
    assert not rows(env, "SELECT * FROM approval_log")


def test_approve_wrong_prefix_and_bad_approver(env):
    code, out = act(env, "job_approve", {"job_id": "good-1", "confirm_prefix": "zzzzzzzzzzzz", "approver": "Ann"})
    assert code == 422 and "confirm_prefix" in out["refused_reason"]
    assert act(env, "job_approve", {"job_id": "good-1", "confirm_prefix": "a" * 11, "approver": "Ann"})[0] == 422
    for bad in ("", "A", "-x", "a;rm", "x" * 41, "Ann\nBob", None):
        assert act(env, "job_approve", {"job_id": "good-1", "confirm_prefix": "a" * 12, "approver": bad})[0] == 400
    assert not env.runner.calls


def test_prefix_read_from_job_json_not_db(env):
    (env.out / "good-1" / "job.json").write_text(json.dumps(
        {"channel": "chan", "status": "rendered", "content_hash": "f" * 64}))
    assert act(env, "job_approve", {"job_id": "good-1", "confirm_prefix": "a" * 12, "approver": "Ann"})[0] == 422
    assert act(env, "job_approve", {"job_id": "good-1", "confirm_prefix": "f" * 12, "approver": "Ann"})[0] == 202


def test_approve_runs_studio_approve_and_logs(env):
    code, out = act(env, "job_approve", {"job_id": "good-1", "confirm_prefix": "a" * 12, "approver": "Ann B."})
    assert code == 202
    assert [c[0] for c in env.runner.calls] == [["/x/.venv/bin/studio", "approve", "good-1", "--by", "Ann B."],
                                                ["/x/.venv/bin/studio", "track", "sync"]]
    log = rows(env, "SELECT * FROM approval_log")
    assert len(log) == 1 and (log[0]["job_id"], log[0]["action"], log[0]["approver"], log[0]["content_hash"]) == (
        "good-1", "approve", "Ann B.", "a" * 64)
    row = get(env, f"/api/actions/{out['id']}")
    assert row["status"] == "ok" and row["actor"] == "Ann B."
    # approved_hash is never written by the dashboard
    assert rows(env, "SELECT approved_hash FROM jobs WHERE job_id='good-1'")[0]["approved_hash"] is None


def test_failed_approve_not_logged_and_skips_sync(env):
    env.runner.exit_code = 1
    _, out = act(env, "job_approve", {"job_id": "good-1", "confirm_prefix": "a" * 12, "approver": "Ann"})
    assert get(env, f"/api/actions/{out['id']}")["status"] == "failed"
    assert len(env.runner.calls) == 1 and not rows(env, "SELECT * FROM approval_log")


def test_reject_is_note_only(env):
    code, _ = act(env, "job_reject", {"job_id": "good-2", "approver": "Ann"})
    assert code == 200 and not env.runner.calls
    log = rows(env, "SELECT * FROM approval_log")
    assert log[0]["action"] == "reject" and log[0]["job_id"] == "good-2"
    assert json.loads((env.out / "good-2" / "job.json").read_text())["status"] == "rendered"
    assert act(env, "job_reject", {"job_id": "nope-1", "approver": "Ann"})[0] == 422


# ---------------------------------------------------------------- bulk
IDS = ["good-1", "good-2", "good-3", "good-4"]


def _bulk(e, ids=IDS, **over):
    body = {"job_ids": ids, "approver": "Ann", "confirm": f"APPROVE {len(ids)}", "sample_viewed": []}
    body.update(over)
    return act(e, "bulk_approve", body)


def test_review_sample_rules(env):
    s = get(env, "/api/review/sample?ids=" + ",".join(IDS))
    assert s["required"] == 3 and set(s["sample"]) <= set(IDS) and s["n"] == 4
    assert get(env, "/api/review/sample?ids=" + ",".join(reversed(IDS)))["sample"] == s["sample"]  # remembered
    assert get(env, "/api/review/sample?ids=good-1")["required"] == 1  # capped at N
    big = [f"j{i}" for i in range(50)]
    assert get(env, "/api/review/sample?ids=" + ",".join(big))["required"] == 5
    with pytest.raises(urllib.error.HTTPError) as ex:
        get(env, "/api/review/sample?ids=../x")
    assert ex.value.code == 400


def test_viewed_validation(env):
    s = get(env, "/api/review/sample?ids=" + ",".join(IDS))
    assert post(env, "/api/review/viewed", {"job_id": s["sample"][0], "seconds": 4})[0] == 400
    assert post(env, "/api/review/viewed", {"job_id": "good-9", "seconds": 9})[0] == 400  # not in sample
    assert post(env, "/api/review/viewed", {"job_id": s["sample"][0], "seconds": 9})[0] == 200
    assert post(env, "/api/review/viewed", {"job_id": s["sample"][0], "seconds": 9}, token=False)[0] == 403


def test_bulk_needs_sample_viewed_and_confirm(env):
    code, out = _bulk(env)
    assert code == 422 and "sample" in out["refused_reason"]
    s = get(env, "/api/review/sample?ids=" + ",".join(IDS))["sample"]
    code, out = _bulk(env, sample_viewed=s)  # claimed, but never marked viewed
    assert code == 422 and "not fully viewed" in out["refused_reason"]
    for j in s[:-1]:
        post(env, "/api/review/viewed", {"job_id": j, "seconds": 6})
    assert _bulk(env, sample_viewed=s)[0] == 422
    post(env, "/api/review/viewed", {"job_id": s[-1], "seconds": 6})
    assert _bulk(env, sample_viewed=s[:-1])[0] == 422  # must include ALL
    assert _bulk(env, sample_viewed=s, confirm="APPROVE 3")[0] == 422
    assert _bulk(env, sample_viewed=s, approver="")[0] == 400
    assert not env.runner.calls


def test_bulk_approves_sequentially_and_logs_sample(env):
    s = get(env, "/api/review/sample?ids=" + ",".join(IDS))["sample"]
    for j in s:
        post(env, "/api/review/viewed", {"job_id": j, "seconds": 5})
    code, _ = _bulk(env, sample_viewed=s)
    assert code == 202
    argvs = [c[0] for c in env.runner.calls]
    assert argvs[:4] == [["/x/.venv/bin/studio", "approve", j, "--by", "Ann"] for j in IDS]
    assert argvs[4] == ["/x/.venv/bin/studio", "track", "sync"]
    log = rows(env, "SELECT * FROM approval_log ORDER BY id")
    assert [r["job_id"] for r in log] == IDS and all(r["action"] == "bulk_approve" for r in log)
    assert json.loads(log[0]["sample_json"]) == s


def test_bulk_refuses_ineligible_member_and_stops_on_failure(env):
    ids = ["good-1", "kid-1", "good-2"]
    s = get(env, "/api/review/sample?ids=" + ",".join(ids))["sample"]
    for j in s:
        post(env, "/api/review/viewed", {"job_id": j, "seconds": 5})
    code, out = _bulk(env, ids, sample_viewed=s)
    assert code == 422 and "kid-1" in out["refused_reason"]
    ids = ["good-1", "good-2", "good-3"]
    s = get(env, "/api/review/sample?ids=" + ",".join(ids))["sample"]
    for j in s:
        post(env, "/api/review/viewed", {"job_id": j, "seconds": 5})
    env.runner.exit_code = 1
    code, out = _bulk(env, ids, sample_viewed=s)
    assert code == 202 and len(env.runner.calls) == 1
    assert get(env, f"/api/actions/{out['id']}")["status"] == "failed" and not rows(env, "SELECT * FROM approval_log")


# ---------------------------------------------------------------- publish dry run
def test_publish_dry_run(env):
    con = pipeline_schema.connect(env.db)
    _job(con, env.out, "appr-ok", valid=1, status="approved")
    con.commit()
    con.close()
    assert act(env, "publish_dry_run", {"job_id": "good-1", "platforms": ["instagram_reels"]})[0] == 422
    assert act(env, "publish_dry_run", {"job_id": "appr-ok", "platforms": ["tiktok"]})[0] == 400
    assert act(env, "publish_dry_run", {"job_id": "appr-ok", "platforms": ["youtube_long"]})[0] == 400
    assert act(env, "publish_dry_run", {"job_id": "appr-ok", "platforms": ["instagram_reels"], "dry_run": False})[0] == 400
    code, _ = act(env, "publish_dry_run", {"job_id": "appr-ok", "platforms": ["instagram_reels", "facebook_reels"]})
    assert code == 202
    argv, child = env.runner.calls[-1]
    assert argv == ["/x/.venv/bin/python", "-m", "studio.dashboard.publish_dry", "appr-ok",
                    "instagram_reels", "facebook_reels"] and child["DRY_RUN"] == "1"
    assert "BUFFER_API_KEY" not in child
