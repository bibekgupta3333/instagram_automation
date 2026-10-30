"""Dashboard actions: a small, strictly bounded write surface (doc 37).

Non-negotiable limits enforced here:
- Real posting is impossible: every child gets ``DRY_RUN=1`` (set last, never from params),
  no credential is passed, no param can change the environment or argv beyond a whitelist.
- No auto-approve: approvals run ONLY ``studio approve JOB --by NAME`` in a subprocess after the
  human's confirm_prefix / confirm text matched; this module never writes ``approved_hash``.
- One action at a time; every request is recorded in ``actions``.
- The DB writer is restricted by a SQLite authorizer to ``actions`` and ``approval_log``.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from studio.pipeline import pipeline_schema

REPO = Path(__file__).resolve().parents[2]
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$")
APPROVER_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9 ._-]{1,39}$")
REGION_RE = re.compile(r"^[A-Za-z]{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LLM_CHOICES = ("opencode", "claude-code", "mock")  # never bedrock / gateway / ollama
BUFFER_PLATFORMS = ("youtube_shorts", "instagram_reels", "facebook_reels")
DAILY_CAP_DEFAULT, DAILY_CAP_CEILING = 20, 1000
DRAFT_MARKER = "DRAFT_NOT_VERIFIED.txt"
SAMPLE_TTL = 30 * 60
MAX_BULK = 100
ACTION_NAMES = ("track_sync", "topics_fetch", "topics_triage", "topics_pick", "plan_run",
                "run_stop", "job_approve", "job_reject", "bulk_approve", "publish_dry_run", "auto")
ENV_PASS = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "STUDIO_ENV_FILE",
            "STUDIO_LEDGER_PATH", "STUDIO_BRAND_DIR", "STUDIO_MOTION", "STUDIO_ZIMAGE_SIZE",
            "STUDIO_DAILY_CAP", "CDN_PUBLIC_BASE_URL", "XDG_CONFIG_HOME", "XDG_DATA_HOME")


class Invalid(Exception):
    """Bad parameters -> HTTP 400."""


class Refused(Exception):
    """Valid request that policy refuses -> HTTP 422."""


@dataclass
class Step:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    pre: Callable[[], str | None] | None = None      # returns a failure reason or None
    after: Callable[[], None] | None = None          # called when the step exits 0


@dataclass
class Plan:
    steps: list[Step]
    actor: str = "dashboard"


class _Handle:
    """Popen wrapper: the child is its own session leader, so only OUR group is signalled."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def wait(self) -> int:
        return self.proc.wait()

    def terminate(self) -> None:
        try:
            if os.getpgid(self.proc.pid) == self.proc.pid:
                os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def default_runner(argv: list[str], env: dict[str, str], log_path: Path, cwd: Path) -> _Handle:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "ab")  # noqa: SIM115 - owned by the child for its lifetime
    try:
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)  # no shell
    finally:
        fh.close()
    return _Handle(proc)


def memory_pressure() -> str:
    try:
        v = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                           capture_output=True, text=True, timeout=3, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return {"1": "green", "2": "yellow", "4": "red"}.get(v, "unknown")


_WRITE_OK = {"actions", "approval_log", "sqlite_sequence"}


def _authorizer(action, arg1, arg2, dbname, source):
    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
        return sqlite3.SQLITE_OK if arg1 in _WRITE_OK else sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                  sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY  # no DDL, no PRAGMA, no ATTACH


def load_plan_entries(path: Path, known: set) -> list[dict]:
    """Read and validate a day plan YAML into {channel, topic, format} entries (shared with
    the auto driver, which re-reads the plan after ``topics pick`` has written it)."""
    import yaml
    try:
        raw = yaml.safe_load(path.read_text()) or []
    except (OSError, yaml.YAMLError) as exc:
        raise Invalid(f"plan unreadable: {exc}") from None
    if not isinstance(raw, list) or not raw:
        raise Invalid("plan is empty")
    entries: list[dict] = []
    for e in raw:
        if not isinstance(e, dict):
            raise Invalid("plan entries must be mappings")
        ch, topic, fmt = e.get("channel"), e.get("topic"), e.get("format") or "short"
        if ch not in known:
            raise Invalid(f"unknown channel {ch!r} in plan")
        if fmt not in ("short", "long"):
            raise Invalid("plan format must be short or long")
        if (not isinstance(topic, str) or not topic.strip() or len(topic) > 300
                or "\n" in topic or "\r" in topic or topic.strip().startswith("#")):
            raise Invalid("plan topic is empty, too long, multi-line or starts with '#'")
        entries.append({"channel": ch, "topic": topic.strip(), "format": fmt})
    return entries


def _log_reason(log_path: Path) -> str | None:
    """A driver that refuses (cap, ...) prints ``[auto] refused: <why>``; surface it as the reason."""
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines[-200:]):
        if ln.startswith("[auto] refused:"):
            return ln[len("[auto] refused:"):].strip()[:300]
    return None


def _bool(p: dict, key: str, default: bool = False) -> bool:
    v = p.get(key, default)
    if not isinstance(v, bool):
        raise Invalid(f"{key} must be a boolean")
    return v


def _int(p: dict, key: str, lo: int, hi: int, default: int | None = None) -> int | None:
    v = p.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise Invalid(f"{key} must be an integer")
    return max(lo, min(hi, v))


def _only(p: dict, allowed: tuple[str, ...]) -> None:
    extra = sorted(set(p) - set(allowed))
    if extra:
        raise Invalid(f"unknown parameter(s): {', '.join(extra)}")


def _job_id(v) -> str:
    if not isinstance(v, str) or not JOB_ID_RE.match(v):
        raise Invalid("invalid job id")
    return v


def _approver(v) -> str:
    if not isinstance(v, str) or not APPROVER_RE.match(v.strip()) or not v.strip():
        raise Invalid("approver must be 2..40 chars of [A-Za-z0-9 ._-] and not start with '-'")
    return v.strip()


class ActionManager:
    def __init__(self, db, output_dir: Path, *, repo: Path = REPO, runner=None,
                 pressure_fn: Callable[[], str] = memory_pressure,
                 brand_loader: Callable[[], Mapping] | None = None,
                 studio_bin: str | None = None, python: str | None = None,
                 plan_dir: Path | None = None, env: Mapping[str, str] | None = None):
        self.db = pipeline_schema.db_path(db)
        self.output_dir = Path(output_dir).resolve()
        self.repo = repo
        self.runner = runner or default_runner
        self.pressure_fn = pressure_fn
        self._brand_loader = brand_loader
        venv = repo / ".venv" / "bin"
        self.studio = studio_bin or str(venv / "studio")
        self.python = python or (str(venv / "python") if (venv / "python").exists() else sys.executable)
        self._plan_dir = plan_dir
        self._env = env
        self.log_dir = self.output_dir / "dashboard-logs"
        self.token = __import__("secrets").token_urlsafe(32)
        self._lock = threading.RLock()
        self._running_id: int | None = None
        self._handle = None
        self._stop_requested = False
        self._samples: dict[tuple, dict] = {}
        self._viewed: dict[str, float] = {}
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ env / config
    def _environ(self) -> Mapping[str, str]:
        return self._env if self._env is not None else os.environ

    def daily_cap(self) -> int:
        try:
            v = int(self._environ().get("STUDIO_DAILY_CAP") or DAILY_CAP_DEFAULT)
        except ValueError:
            v = DAILY_CAP_DEFAULT
        return max(1, min(v, DAILY_CAP_CEILING))

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        src = self._environ()
        env = {k: src[k] for k in ENV_PASS if k in src}
        env.update({"STUDIO_PIPELINE_DB": str(self.db), "STUDIO_OUTPUT_DIR": str(self.output_dir),
                    "PYTHONUNBUFFERED": "1"})
        for k, v in (extra or {}).items():
            if k in ("DRY_RUN", "STUDIO_ALLOW_MODEL_DOWNLOAD") or k.startswith(("AWS_", "BUFFER_")):
                continue
            env[k] = v
        env.pop("STUDIO_ALLOW_MODEL_DOWNLOAD", None)
        env["DRY_RUN"] = "1"  # last word: real posting is never possible from the dashboard
        return env

    def brands(self) -> Mapping:
        if self._brand_loader:
            return self._brand_loader()
        from studio.config import load_brands
        return load_brands()

    # ------------------------------------------------------------ db
    def _read(self, sql: str, args: tuple = ()) -> list[dict]:
        if not self.db.exists():
            return []
        try:
            con = pipeline_schema.connect(self.db, readonly=True)
        except sqlite3.Error:
            return []
        try:
            return [dict(r) for r in con.execute(sql, args)]
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def _write(self, sql: str, args: tuple = ()) -> int:
        con = pipeline_schema.connect(self.db)  # creates tables if needed, then restrict
        con.set_authorizer(_authorizer)
        try:
            with con:
                return con.execute(sql, args).lastrowid
        finally:
            con.close()

    def _insert_action(self, name, params, actor, status, reason=None, argv=None) -> int:
        aid = self._write(
            "INSERT INTO actions(name,params_json,actor,status,refused_reason,argv_json,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (name, json.dumps(params, default=str), actor, status, reason,
             json.dumps(argv) if argv else None, time.time()))
        return aid

    def _update_action(self, aid: int, **f) -> None:
        cols = ", ".join(f"{k}=?" for k in f)
        self._write(f"UPDATE actions SET {cols} WHERE id=?", (*f.values(), aid))

    def _log_approval(self, job_id, action, approver, content_hash, sample=None) -> None:
        self._write("INSERT INTO approval_log(job_id,action,approver,content_hash,sample_json,"
                    "created_at) VALUES(?,?,?,?,?,?)",
                    (job_id, action, approver, content_hash,
                     json.dumps(sample) if sample is not None else None, time.time()))

    @staticmethod
    def _row(r: dict) -> dict:
        r = dict(r)
        for k, out in (("params_json", "params"), ("argv_json", "argv")):
            try:
                r[out] = json.loads(r.pop(k)) if r.get(k) else None
            except ValueError:
                r[out] = None
        return r

    def list_actions(self, limit: int = 50) -> list[dict]:
        return [self._row(r) for r in self._read(
            "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))]

    def get_action(self, aid: int) -> dict | None:
        rows = self._read("SELECT * FROM actions WHERE id=?", (aid,))
        return self._row(rows[0]) if rows else None

    def action_log(self, aid: int, tail: int = 200) -> dict | None:
        row = self.get_action(aid)
        if not row:
            return None
        lines: list[str] = []
        lp = row.get("log_path")
        if lp:
            p = Path(os.path.realpath(lp))
            try:
                p.relative_to(self.log_dir.resolve())
                lines = p.read_text(errors="replace").splitlines()[-max(1, min(tail, 2000)):]
            except (ValueError, OSError):
                lines = []
        return {"id": aid, "status": row["status"], "lines": lines}

    # ------------------------------------------------------------ job eligibility
    def _job_json_path(self, job_id: str) -> Path | None:
        p = Path(os.path.realpath(self.output_dir / job_id / "job.json"))
        try:
            p.relative_to(self.output_dir)
        except ValueError:
            return None
        return p if p.is_file() else None

    def approve_blocker(self, job_id: str) -> tuple[str | None, str | None]:
        """(reason it cannot be approved, CURRENT content_hash from job.json)."""
        rows = self._read("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not rows:
            return "job not in the tracking DB (run track sync)", None
        j = rows[0]
        if j.get("status") != "rendered":
            return f"job status is {j.get('status')!r}; only 'rendered' jobs can be approved", None
        if j.get("error") or j.get("status") == "failed":
            return "job failed", None
        if j.get("is_draft"):
            return "draft job (DRAFT_NOT_VERIFIED)", None
        if j.get("approval_valid") == 1:
            return "already approved with a valid approval", None
        jp = self._job_json_path(job_id)
        if not jp:
            return "output/<job>/job.json not found inside output/", None
        if (jp.parent / DRAFT_MARKER).exists():
            return "draft job (DRAFT_NOT_VERIFIED.txt present)", None
        try:
            data = json.loads(jp.read_text())
        except (OSError, ValueError):
            return "job.json unreadable", None
        if data.get("status") != "rendered":
            return f"job.json status is {data.get('status')!r}, not 'rendered'", None
        h = data.get("content_hash")
        if not h:
            return "job.json has no content_hash", None
        try:
            brand = self.brands().get(data.get("channel") or j.get("channel"))
        except Exception as exc:  # noqa: BLE001
            return f"brand packs unavailable: {exc}", None
        if brand is None:
            return "unknown channel brand pack", None
        if getattr(brand, "made_for_kids", False):
            return "kids channel: approval is CLI-only (private upload path)", None
        gates = {g["gate"]: g["passed"] for g in self._read(
            "SELECT gate,passed FROM gate_results WHERE job_id=?", (job_id,))}
        if any(v != 1 for v in gates.values()):
            return "a gate failed", None
        for need in ("policy", "fact_check"):
            if gates.get(need) != 1:
                return f"gate {need} has not passed", None
        return None, h

    # ------------------------------------------------------------ review sample
    def review_sample(self, ids: list[str]) -> dict:
        ids = [_job_id(i) for i in ids]
        key = tuple(sorted(set(ids)))
        if not key or len(key) > MAX_BULK:
            raise Invalid(f"ids must contain 1..{MAX_BULK} unique job ids")
        now = time.time()
        with self._lock:
            self._samples = {k: v for k, v in self._samples.items() if v["expires"] > now}
            ent = self._samples.get(key)
            if not ent:
                n = len(key)
                k = min(n, max(3, math.ceil(n * 0.10)))
                ent = {"sample": sorted(random.SystemRandom().sample(key, k)),
                       "expires": now + SAMPLE_TTL}
                self._samples[key] = ent
            return {"n": len(key), "required": len(ent["sample"]), "sample": ent["sample"],
                    "expires_at": ent["expires"], "ttl_seconds": SAMPLE_TTL}

    def mark_viewed(self, job_id, seconds) -> dict:
        job_id = _job_id(job_id)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 5:
            raise Invalid("seconds must be a number >= 5")
        now = time.time()
        with self._lock:
            live = any(v["expires"] > now and job_id in v["sample"] for v in self._samples.values())
            if not live:
                raise Invalid("job is not in an active review sample")
            self._viewed[job_id] = max(self._viewed.get(job_id, 0), float(seconds))
        return {"job_id": job_id, "viewed": True}

    # ------------------------------------------------------------ submit
    def submit(self, name: str, params: dict) -> tuple[int, dict]:
        actor = "dashboard"
        a = params.get("approver")
        if isinstance(a, str) and APPROVER_RE.match(a.strip() or "-"):
            actor = a.strip()
        with self._lock:
            if name == "run_stop":
                return self._stop(params)
            if name == "job_reject":
                return self._reject(params, actor)
            if self._running_id is not None:
                aid = self._insert_action(name, params, actor, "refused",
                                          f"busy: action {self._running_id} is running")
                return 409, {"id": aid, "status": "refused", "error": "busy",
                             "running_id": self._running_id,
                             "refused_reason": f"action {self._running_id} is running"}
            try:
                plan = getattr(self, f"_plan_{name}")(params)
            except Invalid as exc:
                aid = self._insert_action(name, params, actor, "refused", f"invalid: {exc}")
                return 400, {"id": aid, "status": "refused", "error": str(exc),
                             "refused_reason": f"invalid: {exc}"}
            except Refused as exc:
                aid = self._insert_action(name, params, actor, "refused", str(exc))
                return 422, {"id": aid, "status": "refused", "error": str(exc),
                             "refused_reason": str(exc)}
            aid = self._insert_action(name, params, plan.actor, "queued",
                                      argv=[s.argv for s in plan.steps])
            log_path = self.log_dir / f"{aid}.log"
            self._update_action(aid, status="running", started_at=time.time(),
                                log_path=str(log_path))
            self._running_id, self._stop_requested = aid, False
            self._thread = threading.Thread(target=self._execute, args=(aid, plan, log_path),
                                            daemon=True)
            self._thread.start()
            return 202, {"id": aid, "status": "running"}

    def wait_idle(self, timeout: float = 10) -> None:
        t = self._thread
        if t:
            t.join(timeout)

    def _execute(self, aid: int, plan: Plan, log_path: Path) -> None:
        status, code, reason = "ok", 0, None
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            for step in plan.steps:
                if step.pre:
                    why = step.pre()
                    if why:
                        status, code, reason = "failed", 1, why
                        break
                with open(log_path, "ab") as fh:
                    fh.write(f"$ {' '.join(step.argv)}\n".encode())
                h = self.runner(step.argv, self.child_env(step.env), log_path, self.repo)
                with self._lock:
                    self._handle = h
                    stop_now = self._stop_requested
                if stop_now:
                    h.terminate()
                code = h.wait()
                if self._stop_requested:
                    status = "stopped"
                    break
                if code != 0:
                    status = "failed"
                    break
                if step.after:
                    step.after()
        except Exception as exc:  # noqa: BLE001 - recorded, never raised into the server
            status, code, reason = "failed", 1, f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._handle, self._running_id = None, None
            if status == "failed" and not reason:
                reason = _log_reason(log_path)
            if reason:
                try:
                    with open(log_path, "ab") as fh:
                        fh.write(f"[dashboard] {reason}\n".encode())
                except OSError:
                    pass
            try:
                self._update_action(aid, status=status, exit_code=code, ended_at=time.time(),
                                    **({"refused_reason": reason} if reason else {}))
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------ inline actions
    def _stop(self, params: dict) -> tuple[int, dict]:
        try:
            _only(params, ())
        except Invalid as exc:
            aid = self._insert_action("run_stop", params, "dashboard", "refused", f"invalid: {exc}")
            return 400, {"id": aid, "status": "refused", "error": str(exc)}
        if self._running_id is None:
            aid = self._insert_action("run_stop", params, "dashboard", "refused", "nothing running")
            return 422, {"id": aid, "status": "refused", "refused_reason": "nothing running",
                         "error": "nothing running"}
        target = self._running_id
        self._stop_requested = True
        if self._handle is not None:
            self._handle.terminate()
        aid = self._insert_action("run_stop", {"target": target}, "dashboard", "ok")
        self._update_action(aid, exit_code=0, started_at=time.time(), ended_at=time.time())
        return 200, {"id": aid, "status": "ok", "stopped_id": target}

    def _reject(self, params: dict, actor: str) -> tuple[int, dict]:
        try:
            _only(params, ("job_id", "approver"))
            job_id, approver = _job_id(params.get("job_id")), _approver(params.get("approver"))
            if not self._read("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)):
                raise Refused("job not in the tracking DB")
        except Invalid as exc:
            aid = self._insert_action("job_reject", params, actor, "refused", f"invalid: {exc}")
            return 400, {"id": aid, "status": "refused", "error": str(exc)}
        except Refused as exc:
            aid = self._insert_action("job_reject", params, actor, "refused", str(exc))
            return 422, {"id": aid, "status": "refused", "error": str(exc), "refused_reason": str(exc)}
        jp = self._job_json_path(job_id)
        h = None
        if jp:
            try:
                h = json.loads(jp.read_text()).get("content_hash")
            except (OSError, ValueError):
                h = None
        aid = self._insert_action("job_reject", params, approver, "ok")
        self._log_approval(job_id, "reject", approver, h)
        self._update_action(aid, exit_code=0, started_at=time.time(), ended_at=time.time())
        return 200, {"id": aid, "status": "ok",
                     "note": "recorded in approval_log only; job.json is untouched (no reject path in the CLI)"}

    # ------------------------------------------------------------ plans (validation -> argv)
    def _plan_track_sync(self, p: dict) -> Plan:
        _only(p, ())
        return Plan([Step([self.studio, "track", "sync"])])

    def _plan_topics_fetch(self, p: dict) -> Plan:
        _only(p, ("source", "region", "mock", "path"))
        source = p.get("source", "trends")
        if source not in ("trends", "library", "file"):
            raise Invalid("source must be trends, library or file")
        region = p.get("region", "US")
        if not isinstance(region, str) or not REGION_RE.match(region):
            raise Invalid("region must be a 2-letter code")
        argv = [self.studio, "topics", "fetch", "--source", source, "--region", region.upper()]
        if source == "file":
            raw = p.get("path")
            if not isinstance(raw, str) or not raw or "\x00" in raw:
                raise Invalid("path is required for source=file")
            real = Path(os.path.realpath(self.output_dir / raw if not os.path.isabs(raw) else raw))
            try:
                real.relative_to(self.output_dir)
            except ValueError:
                raise Invalid("path must resolve inside output/") from None
            if not real.is_file():
                raise Invalid("path is not a file")
            argv += ["--file", str(real)]
        elif "path" in p:
            raise Invalid("path is only valid with source=file")
        if _bool(p, "mock"):
            argv.append("--mock")
        return Plan([Step(argv)])

    def _llm(self, p: dict, required: bool) -> str | None:
        llm = p.get("llm")
        if llm is None:
            if required:
                raise Invalid("llm is required: opencode | claude-code | mock")
            return None
        if llm not in LLM_CHOICES:
            raise Invalid("llm must be opencode, claude-code or mock")
        return llm

    def _plan_topics_triage(self, p: dict) -> Plan:
        _only(p, ("limit", "mock_llm", "llm"))
        mock_llm = _bool(p, "mock_llm")
        llm = self._llm(p, required=not mock_llm)
        argv = [self.studio, "topics", "triage"]
        limit = _int(p, "limit", 1, 1000)
        if limit:
            argv += ["--limit", str(limit)]
        if mock_llm:
            argv.append("--mock-llm")
            llm = "mock"
        return Plan([Step(argv, {"STUDIO_LLM": llm})])

    def _plan_topics_pick(self, p: dict) -> Plan:
        _only(p, ("daily_cap", "per_channel"))
        cap = _int(p, "daily_cap", 1, DAILY_CAP_CEILING, self.daily_cap())
        per = _int(p, "per_channel", 1, 50, 1)
        return Plan([Step([self.studio, "topics", "pick", "--per-channel", str(per),
                           "--daily-cap", str(cap)])])

    def _plan_dir_path(self) -> Path:
        if self._plan_dir:
            return Path(self._plan_dir)
        return Path(self._environ().get("STUDIO_PLAN_DIR") or self.output_dir / "plans")

    def _plan_plan_run(self, p: dict) -> Plan:
        _only(p, ("date", "mock", "llm", "max_jobs"))
        from studio.dashboard import plan_driver

        day = p.get("date") or date.today().isoformat()
        if not isinstance(day, str) or not DATE_RE.match(day):
            raise Invalid("date must be YYYY-MM-DD")
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            raise Invalid("date is not a real calendar date") from None
        mock = _bool(p, "mock")
        llm = self._llm(p, required=not mock) or "mock"
        max_jobs = _int(p, "max_jobs", 1, DAILY_CAP_CEILING)
        path = Path(os.path.realpath(self._plan_dir_path() / f"{day}.yaml"))
        if not path.is_file():
            raise Invalid(f"no day plan at {path.name}; run topics pick first")
        entries = load_plan_entries(path, set(self.brands()))
        if max_jobs:
            entries = entries[:max_jobs]
        today = datetime.now().strftime("%Y-%m-%d")
        n_today = self._read("SELECT COUNT(*) n FROM jobs WHERE "
                             "date(created_at,'unixepoch','localtime')=?", (today,))
        used, cap = (n_today[0]["n"] if n_today else 0), self.daily_cap()
        if used + len(entries) > cap:
            raise Refused(f"daily cap {cap}: {used} job(s) today + {len(entries)} planned exceeds it")
        if not mock:
            lvl = self.pressure_fn()
            if lvl != "green":
                raise Refused(f"memory pressure is {lvl}, not green: real runs are refused")
        tag = f"plan-{day}-{int(time.time())}"
        invs = plan_driver.build_invocations(
            entries, python=self.python, studio=self.studio,
            run_guarded=str(self.repo / "scripts" / "run_guarded.py"), topics_dir=self.log_dir,
            tag=tag, mock=mock, llm=llm)
        manifest = self.log_dir / f"{tag}.manifest.json"
        manifest.write_text(json.dumps({"invocations": invs}))
        return Plan([Step([self.python, "-m", "studio.dashboard.plan_driver", "--manifest",
                           str(manifest), "--cwd", str(self.repo)],
                          {"STUDIO_LLM": llm or "mock"})])

    def _jobs_today(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        n = self._read("SELECT COUNT(*) n FROM jobs WHERE date(created_at,'unixepoch','localtime')=?",
                       (today,))
        return n[0]["n"] if n else 0

    def _plan_auto(self, p: dict) -> Plan:
        """fetch -> triage -> pick -> run in ONE driver process (studio/dashboard/auto_driver.py).
        Never approves or publishes: no such step exists and no parameter can add one."""
        _only(p, ("source", "region", "mock", "path", "llm", "mock_llm", "limit", "daily_cap",
                  "per_channel", "max_jobs", "run"))
        mock = _bool(p, "mock", True)
        run = _bool(p, "run", True)
        mock_llm = _bool(p, "mock_llm")
        llm = self._llm(p, required=not mock) or "mock"
        mock_llm = mock_llm or llm == "mock"  # the offline keyword triager, never a backend
        fetch = self._plan_topics_fetch({k: p[k] for k in ("source", "region", "path") if k in p}
                                        | {"mock": mock}).steps[0]
        triage = self._plan_topics_triage(
            {**{k: p[k] for k in ("limit",) if k in p}, "llm": llm, "mock_llm": mock_llm}).steps[0]
        cap = _int(p, "daily_cap", 1, DAILY_CAP_CEILING, self.daily_cap())
        pick = self._plan_topics_pick({"daily_cap": cap, **{k: p[k] for k in ("per_channel",)
                                                            if k in p}}).steps[0]
        max_jobs = _int(p, "max_jobs", 1, DAILY_CAP_CEILING)
        if run:
            used = self._jobs_today()
            if used >= cap:
                raise Refused(f"daily cap {cap}: {used} job(s) already created today")
            if not mock:
                lvl = self.pressure_fn()
                if lvl != "green":
                    raise Refused(f"memory pressure is {lvl}, not green: real runs are refused")
        tag = f"auto-{int(time.time())}"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.log_dir / f"{tag}.manifest.json"
        manifest.write_text(json.dumps({
            "steps": {"fetch": fetch.argv, "triage": triage.argv, "pick": pick.argv,
                      "sync": [self.studio, "track", "sync"]},
            "plan_dir": str(self._plan_dir_path()), "db": str(self.db), "cap": cap,
            "max_jobs": max_jobs, "mock": mock, "llm": llm, "run": run, "tag": tag,
            "python": self.python, "studio": self.studio,
            "run_guarded": str(self.repo / "scripts" / "run_guarded.py"),
            "topics_dir": str(self.log_dir)}))
        return Plan([Step([self.python, "-m", "studio.dashboard.auto_driver", "--manifest",
                           str(manifest), "--cwd", str(self.repo)],
                          {"STUDIO_LLM": llm, "STUDIO_PLAN_DIR": str(self._plan_dir_path())})])

    def _approve_step(self, job_id: str, approver: str, h: str, *, bulk: bool = False,
                      sample: list[str] | None = None) -> Step:
        def pre() -> str | None:
            why, cur = self.approve_blocker(job_id)
            if why:
                return f"{job_id}: no longer approvable: {why}"
            if cur != h:
                return f"{job_id}: content_hash changed since the request"
            return None

        def after() -> None:
            self._log_approval(job_id, "bulk_approve" if bulk else "approve", approver, h, sample)

        return Step([self.studio, "approve", job_id, "--by", approver], pre=pre, after=after)

    def _plan_job_approve(self, p: dict) -> Plan:
        _only(p, ("job_id", "confirm_prefix", "approver"))
        job_id, approver = _job_id(p.get("job_id")), _approver(p.get("approver"))
        prefix = p.get("confirm_prefix")
        if not isinstance(prefix, str):
            raise Invalid("confirm_prefix is required")
        why, h = self.approve_blocker(job_id)
        if why:
            raise Refused(why)
        if prefix != h[:12]:
            raise Refused("confirm_prefix does not match the current content_hash")
        return Plan([self._approve_step(job_id, approver, h),
                     Step([self.studio, "track", "sync"])], actor=approver)

    def _plan_bulk_approve(self, p: dict) -> Plan:
        _only(p, ("job_ids", "approver", "confirm", "sample_viewed"))
        ids = p.get("job_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BULK:
            raise Invalid(f"job_ids must be a list of 1..{MAX_BULK} ids")
        ids = [_job_id(i) for i in ids]
        if len(set(ids)) != len(ids):
            raise Invalid("job_ids contains duplicates")
        approver = _approver(p.get("approver"))
        if p.get("confirm") != f"APPROVE {len(ids)}":
            raise Refused(f"confirm must be exactly 'APPROVE {len(ids)}'")
        sv = p.get("sample_viewed")
        if not isinstance(sv, list):
            raise Invalid("sample_viewed must be a list of job ids")
        sv = {_job_id(i) for i in sv}
        ent = self._samples.get(tuple(sorted(ids)))
        if not ent or ent["expires"] <= time.time():
            raise Refused("no live review sample for these ids; GET /api/review/sample first")
        missing = [i for i in ent["sample"] if i not in sv or i not in self._viewed]
        if missing:
            raise Refused("sample not fully viewed: " + ", ".join(missing))
        hashes, bad = {}, []
        for i in ids:
            why, h = self.approve_blocker(i)
            if why:
                bad.append(f"{i}: {why}")
            else:
                hashes[i] = h
        if bad:
            raise Refused("not approvable: " + "; ".join(bad))
        steps = [self._approve_step(i, approver, hashes[i], bulk=True, sample=ent["sample"])
                 for i in ids]
        steps.append(Step([self.studio, "track", "sync"]))
        return Plan(steps, actor=approver)

    def _plan_publish_dry_run(self, p: dict) -> Plan:
        _only(p, ("job_id", "platforms"))
        job_id = _job_id(p.get("job_id"))
        plats = p.get("platforms")
        if (not isinstance(plats, list) or not plats or len(set(plats)) != len(plats)
                or any(x not in BUFFER_PLATFORMS for x in plats)):
            raise Invalid(f"platforms must be a unique non-empty subset of {list(BUFFER_PLATFORMS)}")
        rows = self._read("SELECT approval_valid FROM jobs WHERE job_id=?", (job_id,))
        if not rows:
            raise Refused("job not in the tracking DB")
        if rows[0]["approval_valid"] != 1:
            raise Refused("job has no valid approval (approval_valid must be 1)")
        if not self._job_json_path(job_id):
            raise Refused("output/<job>/job.json not found inside output/")
        return Plan([Step([self.python, "-m", "studio.dashboard.publish_dry", job_id, *plats])])
