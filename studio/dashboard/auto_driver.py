"""One-action auto pipeline: fetch trends -> triage -> pick -> run the day plan (doc 37).

Runs as the child of the dashboard's ``auto`` action. Every step is a subprocess built from the
argv the dashboard already uses for the individual actions (manifest written by
``ActionManager._plan_auto``); nothing here uses a shell. It never approves or publishes: there
is no such step, and any argv containing an approve/publish token is refused.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

GUARD_EXIT = 75  # run_guarded.py: lock held, telemetry unavailable or pressure not green
EXIT_REFUSED = 3
EXIT_BAD_MANIFEST = 2
EXIT_STOPPED = 143
STEPS = ("fetch", "triage", "pick", "run")
FORBIDDEN = {"approve", "bulk_approve", "publish", "publish_dry_run", "--by", "approved_hash"}
NEXT_LINE = ("Next: review and approve in the dashboard (Pipeline tab / Review queue). "
             "Nothing was approved or published.")
NOTHING = "nothing to run"

Runner = Callable[[list[str], dict[str, str]], tuple[int, str]]


def say(msg: str) -> None:
    print(msg, flush=True)


def step_env(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    env = {**base, **extra}
    for k in list(env):
        if k.startswith(("AWS_", "BUFFER_")) or k == "STUDIO_ALLOW_MODEL_DOWNLOAD":
            del env[k]
    env["DRY_RUN"] = "1"  # last word, always
    return env


def check_argv(argv: object) -> None:
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    bad = FORBIDDEN & {a.lower() for a in argv}
    if bad:
        raise ValueError(f"refusing an argv with {sorted(bad)}: auto never approves or publishes")


def make_popen_runner(cwd: Path, state: dict) -> Runner:
    def runner(argv: list[str], env: dict[str, str]) -> tuple[int, str]:
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,  # no shell
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace")
        state["proc"] = proc
        tail: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)
            tail.append(line)
            del tail[:-300]
        rc = proc.wait()
        state["proc"] = None
        return rc, "".join(tail)
    return runner


def _int_after(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def parse_fetch(out: str) -> dict:
    return {"fetched": _int_after(r"fetched (\d+) item", out),
            "new": _int_after(r"(\d+) new candidate", out)}


def parse_triage(out: str) -> dict:
    m = re.search(r"triaged (\d+): (\d+) proposed, (\d+) rejected", out)
    classes = {k: int(n) for k, n in re.findall(r"rejected class (\S+): (\d+)", out)}
    return {"triaged": int(m.group(1)) if m else None, "proposed": int(m.group(2)) if m else None,
            "rejected": int(m.group(3)) if m else None, "classes": classes}


def parse_pick(out: str) -> int | None:
    return _int_after(r"picked (\d+)", out)


def jobs_today(db: str) -> int:
    from studio.pipeline import pipeline_schema
    try:
        con = pipeline_schema.connect(db, readonly=True)
    except Exception:  # noqa: BLE001 - no DB yet means no jobs
        return 0
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        return con.execute("SELECT COUNT(*) FROM jobs WHERE "
                           "date(created_at,'unixepoch','localtime')=?", (today,)).fetchone()[0]
    except Exception:  # noqa: BLE001
        return 0
    finally:
        con.close()


def job_outcomes(db: str, since: float) -> tuple[int, int] | None:
    """(rendered, failed) among jobs touched since ``since``, read-only; None if unavailable."""
    from studio.pipeline import pipeline_schema
    try:
        con = pipeline_schema.connect(db, readonly=True)
        try:
            rows = con.execute("SELECT status, COUNT(*) FROM jobs WHERE updated_at>=? "
                               "GROUP BY status", (since,)).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None
    by = {r[0]: r[1] for r in rows}
    return by.get("rendered", 0) + by.get("approved", 0), by.get("failed", 0)


def _summary(s: dict, result: str) -> None:
    def v(k):
        return "-" if s.get(k) is None else s[k]
    cls = s.get("classes") or {}
    say("[auto] ===== summary =====")
    say(f"[auto] result: {result}")
    say(f"[auto] fetched: {v('fetched')}")
    say(f"[auto] new candidates: {v('new')}")
    say(f"[auto] proposed: {v('proposed')}")
    say(f"[auto] rejected: {v('rejected')}" + (
        " (" + ", ".join(f"{k}: {n}" for k, n in sorted(cls.items())) + ")" if cls else ""))
    say(f"[auto] picked: {v('picked')}")
    say(f"[auto] jobs run: {v('jobs_run')}")
    say(f"[auto] jobs rendered: {v('rendered')}")
    say(f"[auto] jobs failed: {v('failed')}")
    say("[auto] ===== end summary =====")
    say(NEXT_LINE)


def run_auto(cfg: dict, *, runner: Runner, base_env: dict[str, str] | None = None,
             brands_fn: Callable[[], set] | None = None, count_today: Callable[[], int] | None = None,
             outcomes: Callable[[float], tuple[int, int] | None] | None = None,
             today: date | None = None, stopped: Callable[[], bool] = lambda: False,
             clock: Callable[[], float] = time.time) -> int:
    base = dict(os.environ if base_env is None else base_env)
    extra = {"STUDIO_LLM": cfg["llm"], "STUDIO_PLAN_DIR": cfg["plan_dir"]}
    steps = cfg["steps"]
    for argv in steps.values():
        check_argv(argv)
    s: dict = {}
    started = clock()
    run = bool(cfg.get("run", True))
    total = 4

    def invoke(n: int, name: str, argv: list[str]) -> tuple[int, str]:
        say(f"[auto] step {n}/{total} {name}")
        say("$ " + " ".join(argv))
        return runner(argv, step_env(base, extra))

    def fail(n: int, name: str, rc: int) -> int:
        say(f"[auto] step {n}/{total} {name} failed (exit {rc}); stopping")
        _summary(s, f"failed at step {n} ({name})")
        return rc

    rc, out = invoke(1, "fetch", steps["fetch"])
    if rc != 0 or stopped():
        return EXIT_STOPPED if stopped() else fail(1, "fetch", rc)
    s.update(parse_fetch(out))

    rc, out = invoke(2, "triage", steps["triage"])
    if rc != 0 or stopped():
        return EXIT_STOPPED if stopped() else fail(2, "triage", rc)
    s.update(parse_triage(out))
    if s.get("proposed") == 0:
        say(f"[auto] {NOTHING}: triage proposed 0 topics")
        s["picked"] = 0
        _summary(s, NOTHING)
        return 0

    rc, out = invoke(3, "pick", steps["pick"])
    if rc != 0 or stopped():
        return EXIT_STOPPED if stopped() else fail(3, "pick", rc)
    s["picked"] = parse_pick(out)
    if s["picked"] == 0:
        say(f"[auto] {NOTHING}: pick selected 0 topics")
        _summary(s, NOTHING)
        return 0
    if not run:
        say("[auto] preview only: nothing was rendered")
        _summary(s, "preview ok")
        return 0

    # ---- step 4: cap check first, then the guarded batches
    day = today or date.today()
    plan = Path(cfg["plan_dir"]) / f"{day.isoformat()}.yaml"
    try:
        from studio.dashboard.actions import Invalid, load_plan_entries
        if brands_fn is None:
            from studio.config import load_brands
            known = set(load_brands())
        else:
            known = brands_fn()
        entries = load_plan_entries(plan, known)
    except Invalid as exc:
        say(f"[auto] {NOTHING}: no runnable day plan ({exc})")
        _summary(s, NOTHING)
        return 0
    if cfg.get("max_jobs"):
        entries = entries[:int(cfg["max_jobs"])]
    used = (count_today or (lambda: jobs_today(cfg["db"])))()
    cap = int(cfg["cap"])
    if used + len(entries) > cap:
        why = f"daily cap {cap}: {used} job(s) today + {len(entries)} planned exceeds it"
        say(f"[auto] refused: {why}")
        _summary(s, "refused (daily cap); nothing was rendered")
        return EXIT_REFUSED
    from studio.dashboard import plan_driver
    invs = plan_driver.build_invocations(
        entries, python=cfg["python"], studio=cfg["studio"], run_guarded=cfg["run_guarded"],
        topics_dir=Path(cfg["topics_dir"]), tag=cfg["tag"], mock=bool(cfg["mock"]), llm=cfg["llm"])
    for inv in invs:
        check_argv(inv["argv"])
    say(f"[auto] step 4/{total} run ({len(entries)} job(s) in {len(invs)} batch(es))")
    worst, guard = 0, False
    for n, inv in enumerate(invs, 1):
        if stopped():
            return EXIT_STOPPED
        say(f"[auto] batch {n}/{len(invs)} {inv['channel']} {inv['format']} x{inv['topics']}")
        say("$ " + " ".join(inv["argv"]))
        rc, _ = runner(inv["argv"], step_env(base, extra))
        if rc == GUARD_EXIT:
            say("[auto] guard stopped the run; not continuing")
            worst, guard = rc, True
            break
        worst = max(worst, rc)
    s["jobs_run"] = len(entries)
    sync_argv = steps["sync"]
    say("[auto] track sync")
    say("$ " + " ".join(sync_argv))
    runner(sync_argv, step_env(base, extra))
    oc = (outcomes or (lambda t: job_outcomes(cfg["db"], t)))(started)
    if oc:
        s["rendered"], s["failed"] = oc
    if stopped():
        return EXIT_STOPPED
    if guard:
        _summary(s, "stopped by the memory guard (exit 75)")
        return GUARD_EXIT
    if worst != 0:
        _summary(s, f"failed at step 4 (run), exit {worst}")
        return worst
    _summary(s, "ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--cwd", type=Path, default=Path.cwd())
    args = ap.parse_args(argv)
    try:
        cfg = json.loads(args.manifest.read_text())
        for k in ("steps", "llm", "plan_dir", "db", "cap", "python", "studio", "run_guarded",
                  "topics_dir", "tag"):
            if k not in cfg:
                raise ValueError(f"manifest missing {k}")
        if not isinstance(cfg["steps"], dict) or set(cfg["steps"]) != {"fetch", "triage", "pick",
                                                                        "sync"}:
            raise ValueError("manifest steps must be fetch, triage, pick, sync")
    except (OSError, ValueError) as exc:
        print(f"auto_driver: bad manifest: {exc}", file=sys.stderr)
        return EXIT_BAD_MANIFEST
    state: dict = {"stop": False, "proc": None}

    def on_term(signum, frame):
        state["stop"] = True
        if state["proc"] is not None:
            state["proc"].terminate()

    signal.signal(signal.SIGTERM, on_term)
    try:
        return run_auto(cfg, runner=make_popen_runner(args.cwd, state),
                        stopped=lambda: state["stop"])
    except ValueError as exc:
        print(f"[auto] refused: {exc}", flush=True)
        return EXIT_BAD_MANIFEST


if __name__ == "__main__":
    raise SystemExit(main())
