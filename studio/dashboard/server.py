"""Dashboard server (stdlib only, loopback only).

All reads use a ``mode=ro`` connection. The only writes are the whitelisted POST routes
(``/api/actions/<name>``, ``/api/review/viewed``) handled by ``actions.ActionManager``, which
needs a per-process session token (``GET /api/session``), a loopback Origin and JSON bodies,
runs one action at a time, always forces ``DRY_RUN=1`` and approves only by running
``studio approve`` for a human-confirmed job. Every other method/route is 405. Media is
served only from inside the repo ``output/`` dir.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from studio.dashboard.actions import ACTION_NAMES, ActionManager, Invalid
from studio.pipeline import pipeline_schema

REPO = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parent / "static"
STAGES = ["script", "fact_check", "tts", "captions", "images", "render"]
EMPTY_HINT = "No data yet. Run `studio track sync` to build output/pipeline.sqlite."
LIMITS = "YouTube ~100 uploads/project/day, Instagram 100/account/day, Facebook 30/Page/day"
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _num(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class Store:
    """All reads. One short-lived read-only connection per call."""

    def __init__(self, db: str | os.PathLike | None, output_dir: Path | None = None):
        self.db = pipeline_schema.db_path(db)
        self.output_dir = (output_dir or REPO / "output").resolve()

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        if not self.db.exists():
            return []
        try:
            con = pipeline_schema.connect(self.db, readonly=True)
        except sqlite3.Error:
            return []
        try:
            return [dict(r) for r in con.execute(sql, args)]
        except sqlite3.Error:  # missing table/column
            return []
        finally:
            con.close()

    def has_data(self) -> bool:
        return bool(self.q("SELECT 1 FROM jobs LIMIT 1") or self.q("SELECT 1 FROM topics LIMIT 1")
                    or self.q("SELECT 1 FROM trend_candidates LIMIT 1"))

    def wrap(self, **data) -> dict:
        out = {"empty": not self.has_data(), **data}
        if out["empty"]:
            out["hint"] = EMPTY_HINT
        return out

    # ---- media safety -------------------------------------------------
    def safe_media(self, raw: str) -> Path | None:
        if not raw or "\x00" in raw:
            return None
        p = Path(raw)
        p = p if p.is_absolute() else REPO / p
        try:
            real = Path(os.path.realpath(p))
            real.relative_to(self.output_dir)
        except (ValueError, OSError):
            return None
        return real if real.is_file() else None

    def media_url(self, raw: str | None) -> str | None:
        if raw and self.safe_media(raw):
            from urllib.parse import quote
            return "/media?path=" + quote(str(raw))
        return None

    # ---- pages --------------------------------------------------------
    def jobs(self, qs: dict) -> dict:
        where, args = [], []
        if qs.get("channel"):
            where.append("channel=?"); args.append(qs["channel"])
        if qs.get("status"):
            where.append("status=?"); args.append(qs["status"])
        if qs.get("draft") in ("0", "1"):
            where.append("is_draft=?"); args.append(int(qs["draft"]))
        w = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.q(f"SELECT job_id,channel,format,topic,status,current_stage,is_draft,approval_valid,"
                      f"approved_by,created_at,updated_at,error FROM jobs{w} "
                      f"ORDER BY COALESCE(updated_at,created_at) DESC LIMIT 500", tuple(args))
        for r in rows:
            r["approval"] = "valid" if r["approval_valid"] == 1 else "invalid" if r["approval_valid"] == 0 else "none"
        return self.wrap(
            jobs=rows,
            channels=[r["channel"] for r in self.q("SELECT DISTINCT channel FROM jobs WHERE channel IS NOT NULL ORDER BY 1")],
            statuses=[r["status"] for r in self.q("SELECT DISTINCT status FROM jobs WHERE status IS NOT NULL ORDER BY 1")],
        )

    def job(self, job_id: str) -> dict:
        rows = self.q("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not rows:
            return {"error": "not found"}
        j = rows[0]
        runs = {r["stage"]: r for r in self.q("SELECT * FROM stage_runs WHERE job_id=?", (job_id,))}
        tiles = [{"stage": s, "status": (runs.get(s) or {}).get("status", "pending"),
                  "seconds": (runs.get(s) or {}).get("seconds"), "error": (runs.get(s) or {}).get("error")}
                 for s in STAGES]
        gates = self.q("SELECT gate,passed,issues_json,created_at FROM gate_results WHERE job_id=?", (job_id,))
        for g in gates:
            g["issues"] = _loads(g.pop("issues_json"))
        assets = self.q("SELECT beat,kind,source,source_id,creator,licence,page_url,local_path FROM assets "
                        "WHERE job_id=? ORDER BY beat", (job_id,))
        for a in assets:
            a["media_url"] = self.media_url(a["local_path"])
        script = None
        wd = j.get("workdir")
        if wd:
            jp = self.safe_media(str(Path(wd) / "job.json"))
            if jp:
                try:
                    script = json.loads(jp.read_text()).get("script")
                except (OSError, ValueError):
                    script = None
        j["approval"] = "valid" if j.get("approval_valid") == 1 else "invalid" if j.get("approval_valid") == 0 else "none"
        return {"job": j, "stages": tiles, "gates": gates, "assets": assets, "script": script,
                "video_url": self.media_url(j.get("video_path")),
                "publishes": self.q("SELECT platform,target,status,remote_id,updated_at FROM publishes WHERE job_id=?", (job_id,)),
                "approve_hint": f"studio approve {job_id} --by <your-name>"}

    def topics(self) -> dict:
        return self.wrap(
            candidates=self.q("SELECT id,term,source,region,volume,day FROM trend_candidates ORDER BY fetched_at DESC LIMIT 200"),
            topics=self.q("SELECT id,channel,format,topic_text,class,status,reject_reason,created_at FROM topics "
                          "ORDER BY created_at DESC LIMIT 300"),
            by_status=self.q("SELECT status, COUNT(*) n FROM topics GROUP BY status ORDER BY n DESC"),
            by_class=self.q("SELECT COALESCE(class,'(none)') class, COUNT(*) n FROM topics GROUP BY 1 ORDER BY n DESC"),
            reject_reasons=self.q("SELECT reject_reason, COUNT(*) n FROM topics WHERE status='rejected' "
                                  "GROUP BY reject_reason ORDER BY n DESC"),
        )

    def gates(self) -> dict:
        fails = self.q("SELECT g.job_id,j.channel,j.topic,g.gate,g.issues_json,g.created_at FROM gate_results g "
                       "LEFT JOIN jobs j ON j.job_id=g.job_id WHERE g.passed=0 "
                       "ORDER BY g.created_at DESC LIMIT 300")
        for f in fails:
            f["issues"] = _loads(f.pop("issues_json"))
        rates = self.q("SELECT COALESCE(j.channel,'(unknown)') channel, g.gate, COUNT(*) total, "
                       "SUM(CASE WHEN g.passed=0 THEN 1 ELSE 0 END) failed FROM gate_results g "
                       "LEFT JOIN jobs j ON j.job_id=g.job_id GROUP BY 1,2 ORDER BY 1,2")
        for r in rates:
            r["failed"] = r["failed"] or 0
            r["rate"] = round(r["failed"] / r["total"], 3) if r["total"] else 0
        return self.wrap(failures=fails, rates=rates)

    def publish(self) -> dict:
        real = "p.target IN ('buffer','youtube_private')"
        return self.wrap(
            publishes=self.q("SELECT p.job_id,j.channel,p.platform,p.target,p.status,p.remote_id,p.updated_at,"
                             "j.approval_valid FROM publishes p LEFT JOIN jobs j ON j.job_id=p.job_id "
                             "ORDER BY p.updated_at DESC LIMIT 300"),
            approved_not_published=self.q(
                f"SELECT job_id,channel,topic,approved_by FROM jobs j WHERE approval_valid=1 AND NOT EXISTS "
                f"(SELECT 1 FROM publishes p WHERE p.job_id=j.job_id AND {real})"),
            published_without_valid_approval=self.q(
                f"SELECT p.job_id,j.channel,p.platform,p.target,p.status,j.approval_valid FROM publishes p "
                f"LEFT JOIN jobs j ON j.job_id=p.job_id WHERE {real} AND COALESCE(j.approval_valid,0)<>1"),
        )

    def batches(self) -> dict:
        return self.wrap(batches=self.q(
            "SELECT b.*, (SELECT COUNT(*) FROM jobs j WHERE j.batch_id=b.id) jobs "
            "FROM batches b ORDER BY COALESCE(started_at,0) DESC, id DESC LIMIT 200"))

    def review(self) -> dict:
        return self.wrap(
            note="Approve from the job panel or the bulk review below (you type the hash prefix and your name; actions are logged), or via CLI: studio approve JOB_ID --by <your-name>.",
            jobs=self.q(
                "SELECT job_id,channel,topic,format,content_hash FROM jobs j "
                "WHERE COALESCE(approval_valid,0)<>1 AND COALESCE(approved_hash,'')='' "
                "AND COALESCE(status,'')<>'failed' "
                "AND NOT EXISTS (SELECT 1 FROM gate_results g WHERE g.job_id=j.job_id AND g.passed=0) "
                "AND EXISTS (SELECT 1 FROM gate_results g WHERE g.job_id=j.job_id AND g.gate='policy' AND g.passed=1) "
                "AND EXISTS (SELECT 1 FROM gate_results g WHERE g.job_id=j.job_id AND g.gate='fact_check' AND g.passed=1) "
                "ORDER BY COALESCE(updated_at,created_at) DESC LIMIT 200"))

    def capacity(self) -> dict:
        cap = _num(os.environ.get("STUDIO_DAILY_CAP"), 20) or 20
        today = datetime.now().strftime("%Y-%m-%d")
        per_day = self.q("SELECT date(created_at,'unixepoch','localtime') day, COUNT(*) n FROM jobs "
                         "WHERE created_at IS NOT NULL GROUP BY 1 ORDER BY 1 DESC LIMIT 30")
        per_ch = self.q("SELECT COALESCE(channel,'(none)') channel, COUNT(*) n FROM jobs "
                        "WHERE date(created_at,'unixepoch','localtime')=? GROUP BY 1 ORDER BY n DESC", (today,))
        today_n = sum(r["n"] for r in per_ch)
        du = shutil.disk_usage(self.output_dir if self.output_dir.exists() else REPO)
        out = self.wrap(cap=cap, today=today, today_count=today_n, per_day=per_day, per_channel_today=per_ch,
                        warning=(f"Daily cap {cap} > 100: platform limits apply ({LIMITS}) and so does policy "
                                 "risk (mass-produced content). Raising the cap does not raise those limits.")
                        if cap > 100 else None,
                        memory_pressure=_pressure(), swap=_sh(["/usr/sbin/sysctl", "-n", "vm.swapusage"]),
                        disk_free_gb=round(du.free / 1e9, 1), spend=self.spend())
        return out

    def spend(self) -> dict | None:
        p = Path(os.environ.get("STUDIO_LEDGER_PATH") or self.output_dir / "ledger.sqlite")
        if not p.exists():
            return None
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                today = datetime.now().strftime("%Y-%m-%d")
                rows = [dict(r) for r in con.execute(
                    "SELECT channel, ROUND(SUM(usd),4) usd, COUNT(*) calls FROM llm_spend "
                    "WHERE substr(ts,1,7)=? GROUP BY channel ORDER BY usd DESC", (today[:7],))]
                t = con.execute("SELECT COALESCE(SUM(usd),0) FROM llm_spend WHERE substr(ts,1,10)=?", (today,)).fetchone()[0]
                return {"month_by_channel": rows, "today_usd": round(t, 4),
                        "month_usd": round(sum(r["usd"] or 0 for r in rows), 4)}
            finally:
                con.close()
        except sqlite3.Error:
            return None

    def live(self) -> dict:
        pids = _sh(["/usr/bin/pgrep", "-f", "run_guarded.py"])
        d = os.environ.get("STUDIO_RUN_LOG")
        newest = None
        if d and Path(d).is_dir():
            files = [f for f in Path(d).iterdir() if f.is_file()]
            if files:
                f = max(files, key=lambda x: x.stat().st_mtime)
                try:
                    tail = f.read_text(errors="replace").splitlines()[-40:]
                except OSError:
                    tail = []
                newest = {"name": f.name, "mtime": f.stat().st_mtime, "tail": tail}
        return {"guarded_running": bool(pids), "pids": pids.split() if pids else [],
                "run_log_dir": d, "newest_log": newest, "now": time.time()}


def _sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _pressure() -> str:
    v = _sh(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
    return {"1": "green", "2": "yellow", "4": "red"}.get(v, "unknown")


def _loads(s):
    if not s:
        return []
    try:
        return json.loads(s)
    except ValueError:
        return [str(s)]


MAX_BODY = 64 * 1024


def make_handler(store: Store, mgr: ActionManager):
    class H(BaseHTTPRequestHandler):
        server_version = "StudioDashboard"

        def log_message(self, *a):  # quiet
            pass

        def _host_ok(self) -> bool:
            h = (self.headers.get("Host") or "").strip()
            name = h.rsplit(":", 1)[0] if not h.startswith("[") else h.split("]")[0] + "]"
            return name in LOOPBACK_NAMES

        def _send(self, code, body: bytes, ctype="application/json", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str).encode())

        def do_GET(self):
            if not self._host_ok():
                return self._json({"error": "bad host"}, 403)
            u = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = unquote(u.path)
            if path == "/":
                return self._static("index.html")
            if path in ("/app.js", "/styles.css"):
                return self._static(path[1:])
            if path == "/media":
                return self._media(qs.get("path", ""))
            routes = {
                "/api/jobs": lambda: store.jobs(qs), "/api/topics": store.topics, "/api/gates": store.gates,
                "/api/publish": store.publish, "/api/capacity": store.capacity,
                "/api/batches": store.batches, "/api/review": store.review, "/api/live": store.live,
            }
            if path == "/api/session":
                return self._json({"token": mgr.token, "dry_run": True, "daily_cap": mgr.daily_cap(),
                                   "actions": list(ACTION_NAMES)})
            if path == "/api/actions":
                return self._json({"actions": mgr.list_actions(_num(qs.get("limit"), 50))})
            m = re.fullmatch(r"/api/actions/(\d{1,12})(/log)?", path)
            if m:
                if m.group(2):
                    r = mgr.action_log(int(m.group(1)), _num(qs.get("tail"), 200))
                else:
                    r = mgr.get_action(int(m.group(1)))
                return self._json(r, 200) if r else self._json({"error": "not found"}, 404)
            if path == "/api/review/sample":
                try:
                    ids = [i for i in qs.get("ids", "").split(",") if i]
                    return self._json(mgr.review_sample(ids))
                except Invalid as exc:
                    return self._json({"error": str(exc)}, 400)
            if path == "/api/job":
                r = store.job(qs.get("id", ""))
                return self._json(r, 404 if r.get("error") else 200)
            fn = routes.get(path)
            if fn:
                return self._json(fn())
            self._json({"error": "not found"}, 404)

        do_HEAD = do_GET

        def _static(self, name):
            ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                     "css": "text/css; charset=utf-8"}[name.rsplit(".", 1)[1]]
            self._send(200, (STATIC / name).read_bytes(), ctype)

        def _media(self, raw):
            p = store.safe_media(raw)
            if not p:
                return self._json({"error": "not found"}, 404)
            size = p.stat().st_size
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            start, end, code = 0, size - 1, 200
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes=") and size:
                try:
                    a, _, b = rng[6:].partition("-")
                    start = int(a) if a else max(0, size - int(b))
                    end = min(int(b), size - 1) if (a and b) else size - 1
                    if start > end or start >= size:
                        raise ValueError
                    code = 206
                except ValueError:
                    return self._send(416, b"", extra={"Content-Range": f"bytes */{size}"})
            with p.open("rb") as f:
                f.seek(start)
                data = f.read(end - start + 1)
            extra = {"Accept-Ranges": "bytes"}
            if code == 206:
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            self._send(code, data, ctype, extra)

        def _deny(self):
            self._send(405, b'{"error":"method not allowed"}', extra={"Allow": "GET, HEAD, POST"})

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            u = urlparse(origin)
            return (u.scheme == "http" and u.hostname is not None and is_loopback(u.hostname)
                    and u.port == self.server.server_address[1])

        def do_POST(self):
            if not self._host_ok():
                return self._json({"error": "bad host"}, 403)
            path = unquote(urlparse(self.path).path)
            m = re.fullmatch(r"/api/actions/([a-z_]{1,32})", path)
            if not m and path != "/api/review/viewed":
                return self._deny()
            if not secrets.compare_digest(self.headers.get("X-Dashboard-Token") or "", mgr.token):
                return self._json({"error": "missing or wrong X-Dashboard-Token"}, 403)
            if not self._origin_ok():
                return self._json({"error": "bad origin"}, 403)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._json({"error": "Content-Type must be application/json"}, 400)
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return self._json({"error": "Content-Length required"}, 400)
            if n < 0:
                return self._json({"error": "bad Content-Length"}, 400)
            if n > MAX_BODY:
                return self._json({"error": "body too large (max 64 KB)"}, 413)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"error": "body must be JSON"}, 400)
            if not isinstance(body, dict):
                return self._json({"error": "body must be a JSON object"}, 400)
            if path == "/api/review/viewed":
                try:
                    return self._json(mgr.mark_viewed(body.get("job_id"), body.get("seconds")))
                except Invalid as exc:
                    return self._json({"error": str(exc)}, 400)
            if m.group(1) not in ACTION_NAMES:
                return self._json({"error": "unknown action"}, 404)
            code, out = mgr.submit(m.group(1), body)
            self._json(out, code)

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _deny

    return H


def make_server(host: str, port: int, db=None, output_dir: Path | None = None,
                manager: ActionManager | None = None) -> ThreadingHTTPServer:
    if not is_loopback(host):
        raise ValueError(f"refusing to bind {host!r}: the dashboard is loopback only")
    store = Store(db, output_dir)
    mgr = manager or ActionManager(store.db, store.output_dir)
    srv = ThreadingHTTPServer((host, port), make_handler(store, mgr))
    srv.daemon_threads = True
    srv.actions = mgr  # type: ignore[attr-defined]
    return srv


def serve(host: str, port: int, db=None) -> None:
    srv = make_server(host, port, db)
    print(f"dashboard on http://{host}:{srv.server_address[1]}/  db={pipeline_schema.db_path(db)}")
    srv.serve_forever()
