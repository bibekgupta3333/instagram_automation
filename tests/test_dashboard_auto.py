"""Auto pipeline (fetch -> triage -> pick -> run): driver and action tests, all offline."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date
from types import SimpleNamespace

import pytest

from studio.dashboard import auto_driver
from studio.dashboard.actions import ActionManager
from studio.pipeline import pipeline_schema

FETCH_OUT = "fetched 12 item(s) from trends; 9 new candidate(s)\n"
TRIAGE_OUT = ("triaged 9: 4 proposed, 5 rejected, 0 deferred\n  rejected class sensitive: 3\n"
              "  rejected class off_brand: 2\n")
PICK_OUT = "picked 2 (cap 20, 0 already planned) -> plan.yaml\n"


class Fake:
    """Fake subprocess runner: records (argv, env); outputs/exit codes keyed by argv shape."""

    def __init__(self, triage=TRIAGE_OUT, pick=PICK_OUT, rc=None):
        self.calls: list[tuple[list[str], dict]] = []
        self.out = {"fetch": FETCH_OUT, "triage": triage, "pick": pick}
        self.rc = rc or {}

    def key(self, argv):
        if "run_guarded.py" in " ".join(argv):
            return "batch"
        for k in ("fetch", "triage", "pick", "sync"):
            if k in argv:
                return k
        return "?"

    def __call__(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        k = self.key(argv)
        return self.rc.get(k, 0), self.out.get(k, "")

    def seq(self):
        return [self.key(a) for a, _ in self.calls]


def cfg_for(tmp_path, **kw):
    plans = tmp_path / "plans"
    plans.mkdir(exist_ok=True)
    (plans / f"{date.today().isoformat()}.yaml").write_text(
        "- {channel: chan, topic: Why ice floats, format: short}\n"
        "- {channel: other, topic: How tides work, format: short}\n")
    c = {"steps": {"fetch": ["/s/studio", "topics", "fetch", "--source", "trends", "--region", "US",
                             "--mock"],
                   "triage": ["/s/studio", "topics", "triage", "--mock-llm"],
                   "pick": ["/s/studio", "topics", "pick", "--per-channel", "1", "--daily-cap", "20"],
                   "sync": ["/s/studio", "track", "sync"]},
         "plan_dir": str(plans), "db": str(tmp_path / "x.sqlite"), "cap": 20, "max_jobs": None,
         "mock": True, "llm": "mock", "run": True, "tag": "auto-1", "python": "/s/python",
         "studio": "/s/studio", "run_guarded": "/s/scripts/run_guarded.py",
         "topics_dir": str(tmp_path / "logs")}
    c.update(kw)
    return c


def drive(tmp_path, fake, capsys=None, **kw):
    cfg = cfg_for(tmp_path, **{k: v for k, v in kw.items() if k in cfg_for(tmp_path)})
    rc = auto_driver.run_auto(cfg, runner=fake, base_env={"PATH": "/usr/bin", "DRY_RUN": "0",
                              "AWS_PROFILE": "x", "STUDIO_ALLOW_MODEL_DOWNLOAD": "1"},
                              brands_fn=lambda: {"chan", "other"},
                              count_today=kw.get("count_today", lambda: 0),
                              outcomes=lambda t: (2, 0))
    return rc


def test_full_run_argv_in_order(tmp_path, capsys):
    f = Fake()
    assert drive(tmp_path, f) == 0
    assert f.seq() == ["fetch", "triage", "pick", "batch", "batch", "sync"]
    argvs = [a for a, _ in f.calls]
    assert argvs[0][:3] == ["/s/studio", "topics", "fetch"] and "--mock" in argvs[0]
    assert argvs[1] == ["/s/studio", "topics", "triage", "--mock-llm"]
    assert argvs[2][:3] == ["/s/studio", "topics", "pick"]
    for b, ch in zip(argvs[3:5], ("chan", "other"), strict=True):
        assert b[:3] == ["/s/python", "/s/scripts/run_guarded.py", "--"]
        assert b[3:7] == ["/s/studio", "batch", "--channel", ch] and "--mock" in b
    assert argvs[5] == ["/s/studio", "track", "sync"]
    out = capsys.readouterr().out
    for n, name in enumerate(("fetch", "triage", "pick", "run"), 1):
        assert f"[auto] step {n}/4 {name}" in out
    assert "[auto] fetched: 12" in out and "[auto] new candidates: 9" in out
    assert "[auto] proposed: 4" in out and "sensitive: 3" in out
    assert "[auto] picked: 2" in out and "[auto] jobs run: 2" in out
    assert "[auto] jobs rendered: 2" in out and "[auto] jobs failed: 0" in out
    assert out.rstrip().endswith("Nothing was approved or published.")


def test_argv_clean_and_dry_run_forced(tmp_path):
    f = Fake()
    drive(tmp_path, f)
    for argv, env in f.calls:
        assert not any("approve" in a or "publish" in a for a in argv)
        assert env["DRY_RUN"] == "1"
        assert not any(k.startswith(("AWS_", "BUFFER_")) for k in env)
        assert "STUDIO_ALLOW_MODEL_DOWNLOAD" not in env
        assert env["STUDIO_LLM"] == "mock"


def test_forbidden_argv_refused(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["steps"]["pick"] = ["/s/studio", "approve", "job1", "--by", "me"]
    with pytest.raises(ValueError):
        auto_driver.run_auto(cfg, runner=Fake(), base_env={})


def test_preview_runs_three_steps(tmp_path, capsys):
    f = Fake()
    assert drive(tmp_path, f, run=False) == 0
    assert f.seq() == ["fetch", "triage", "pick"]
    out = capsys.readouterr().out
    assert "step 4/4" not in out and "preview" in out


@pytest.mark.parametrize("bad,code", [("fetch", 1), ("triage", 2), ("pick", 4)])
def test_stops_at_first_failure(tmp_path, bad, code, capsys):
    f = Fake(rc={bad: code})
    assert drive(tmp_path, f) == code
    order = ["fetch", "triage", "pick"]
    assert f.seq() == order[:order.index(bad) + 1]
    assert "failed at step" in capsys.readouterr().out


def test_zero_proposed_is_ok_and_runs_nothing(tmp_path, capsys):
    f = Fake(triage="triaged 3: 0 proposed, 3 rejected, 0 deferred\n")
    assert drive(tmp_path, f) == 0
    assert f.seq() == ["fetch", "triage"]
    assert "nothing to run" in capsys.readouterr().out


def test_zero_picked_is_ok(tmp_path, capsys):
    f = Fake(pick="picked 0 (cap 20, 0 already planned) -> p\n")
    assert drive(tmp_path, f) == 0
    assert f.seq() == ["fetch", "triage", "pick"]
    assert "nothing to run" in capsys.readouterr().out


def test_guard_exit_75_propagates(tmp_path, capsys):
    f = Fake(rc={"batch": 75})
    assert drive(tmp_path, f) == 75
    assert f.seq() == ["fetch", "triage", "pick", "batch", "sync"]  # no further batches
    assert "memory guard" in capsys.readouterr().out


def test_cap_refused_before_rendering(tmp_path, capsys):
    f = Fake()
    assert drive(tmp_path, f, count_today=lambda: 19) != 0
    assert "batch" not in f.seq() and "sync" not in f.seq()
    out = capsys.readouterr().out
    assert "[auto] refused: daily cap 20" in out and "nothing was rendered" in out


def test_max_jobs_limits_plan_for_cap(tmp_path):
    f = Fake()
    assert drive(tmp_path, f, max_jobs=1, count_today=lambda: 19) == 0
    assert f.seq().count("batch") == 1


# ---------------------------------------------------------------- the action (server side)
class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, env, log_path, cwd):
        self.calls.append((list(argv), dict(env)))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(b"")
        return SimpleNamespace(wait=lambda: 0, terminate=lambda: None)


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.delenv("STUDIO_DAILY_CAP", raising=False)
    out = tmp_path / "output"
    out.mkdir()
    db = out / "pipeline.sqlite"
    pipeline_schema.connect(db).close()
    rec = Rec()
    m = ActionManager(db, out, repo=tmp_path, runner=rec, pressure_fn=lambda: "green",
                      brand_loader=lambda: {"chan": SimpleNamespace(made_for_kids=False)},
                      studio_bin="/x/studio", python="/x/python", plan_dir=tmp_path / "plans",
                      env={"PATH": "/usr/bin", "AWS_PROFILE": "e", "DRY_RUN": "0"})
    m.rec = rec
    return m


def _jobs(mgr, n):
    con = sqlite3.connect(mgr.db)
    for i in range(n):
        con.execute("INSERT INTO jobs(job_id,channel,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (f"j{i}", "chan", "rendered", time.time(), time.time()))
    con.commit()
    con.close()


def test_unknown_param_400(mgr):
    for bad in ({"approve": True}, {"approved_hash": "x"}, {"by": "me"}, {"publish": True}):
        code, out = mgr.submit("auto", bad)
        assert code == 400 and "unknown parameter" in out["error"]
    assert mgr.rec.calls == []


def test_defaults_mock_and_manifest(mgr):
    code, _out = mgr.submit("auto", {})
    assert code == 202
    mgr.wait_idle()
    argv, env = mgr.rec.calls[0]
    assert argv[:3] == ["/x/python", "-m", "studio.dashboard.auto_driver"]
    assert env["DRY_RUN"] == "1" and env["STUDIO_LLM"] == "mock"
    man = json.loads(next((mgr.log_dir).glob("auto-*.manifest.json")).read_text())
    assert man["mock"] is True and man["run"] is True and man["cap"] == 20
    assert man["steps"]["fetch"] == ["/x/studio", "topics", "fetch", "--source", "trends",
                                     "--region", "US", "--mock"]
    assert man["steps"]["triage"] == ["/x/studio", "topics", "triage", "--mock-llm"]
    assert man["steps"]["pick"] == ["/x/studio", "topics", "pick", "--per-channel", "1",
                                    "--daily-cap", "20"]
    assert man["steps"]["sync"] == ["/x/studio", "track", "sync"]
    assert not any("approve" in a or "publish" in a for s in man["steps"].values() for a in s)


@pytest.mark.parametrize("body", [{"mock": False}, {"mock": False, "llm": "bedrock"},
                                  {"llm": "aws"}, {"source": "file"}, {"region": "USA"},
                                  {"run": "yes"}, {"limit": "5"}])
def test_invalid_params_400(mgr, body):
    assert mgr.submit("auto", body)[0] == 400


def test_file_source_must_stay_in_output(mgr, tmp_path):
    (tmp_path / "evil.txt").write_text("x")
    assert mgr.submit("auto", {"source": "file", "path": str(tmp_path / "evil.txt")})[0] == 400
    (mgr.output_dir / "t.txt").write_text("x")
    assert mgr.submit("auto", {"source": "file", "path": "t.txt"})[0] == 202
    mgr.wait_idle()


def test_pressure_refusal_for_real_runs(mgr):
    mgr.pressure_fn = lambda: "unknown"
    code, out = mgr.submit("auto", {"mock": False, "llm": "opencode"})
    assert code == 422 and "not green" in out["refused_reason"]
    assert mgr.rec.calls == []
    # preview does not render, so it is allowed
    assert mgr.submit("auto", {"mock": False, "llm": "opencode", "run": False})[0] == 202
    mgr.wait_idle()


def test_cap_refusal_at_submit(mgr):
    _jobs(mgr, 3)
    code, out = mgr.submit("auto", {"daily_cap": 2})
    assert code == 422 and "daily cap" in out["refused_reason"]
    assert mgr.rec.calls == []
    row = mgr.list_actions()[0]
    assert row["status"] == "refused" and row["name"] == "auto"


def test_real_run_params_reach_steps(mgr):
    assert mgr.submit("auto", {"mock": False, "llm": "opencode", "limit": 7, "per_channel": 2,
                               "daily_cap": 5, "max_jobs": 3, "region": "gb"})[0] == 202
    mgr.wait_idle()
    _argv, env = mgr.rec.calls[0]
    assert env["STUDIO_LLM"] == "opencode"
    man = json.loads(next(mgr.log_dir.glob("auto-*.manifest.json")).read_text())
    assert man["steps"]["fetch"][-1] == "GB" and "--mock" not in man["steps"]["fetch"]
    assert man["steps"]["triage"] == ["/x/studio", "topics", "triage", "--limit", "7", "--mock-llm"] or \
        man["steps"]["triage"] == ["/x/studio", "topics", "triage", "--limit", "7"]
    assert man["steps"]["pick"][-4:] == ["--per-channel", "2", "--daily-cap", "5"]
    assert man["max_jobs"] == 3 and man["cap"] == 5 and man["mock"] is False


def test_driver_refusal_becomes_reason(mgr, tmp_path):
    class R(Rec):
        def __call__(self, argv, env, log_path, cwd):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_bytes(b"[auto] refused: daily cap 2: 1 job(s) today + 3 planned exceeds it\n")
            return SimpleNamespace(wait=lambda: 3, terminate=lambda: None)
    mgr.runner = R()
    assert mgr.submit("auto", {})[0] == 202
    mgr.wait_idle()
    row = mgr.list_actions()[0]
    assert row["status"] == "failed" and "daily cap 2" in row["refused_reason"]
