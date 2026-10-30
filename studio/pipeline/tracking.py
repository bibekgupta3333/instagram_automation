"""Rebuild the tracking DB (output/pipeline.sqlite) from job.json files (doc 37 §6).

``job.json`` is the source of truth; this module only mirrors it (plus the publish ledger,
opened read-only) into the shared schema in ``pipeline_schema``. Nothing here writes job.json,
the ledger, or any approval field. ``sync_from_jobs`` is idempotent and rebuildable.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from studio.pipeline import pipeline_schema as schema

STAGE_ORDER = ["script", "fact_check", "tts", "captions", "images", "render"]
DRAFT_MARKER = "DRAFT_NOT_VERIFIED.txt"
_FAIL_RE = re.compile(r"\b(\w+): FAILED: (.*)", re.DOTALL)


# ------------------------------------------------------------------ helpers
def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ts(value: Any, fallback: float | None = None) -> float | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass
    return fallback


def _approval_valid(data: dict) -> int | None:
    """Mirror runner.verify_approval (read-only); None when it cannot be evaluated."""
    if not data.get("approved_hash"):
        return None
    try:
        from studio.contracts import VideoJob
        from studio.pipeline.runner import verify_approval

        return int(bool(verify_approval(VideoJob.model_validate(data))))
    except Exception:  # noqa: BLE001
        return None


def _produced(data: dict, workdir: Path, draft: bool) -> dict[str, bool]:
    return {
        "script": bool(data.get("script")),
        "fact_check": bool(data.get("fact_check")) or draft,
        "tts": bool(data.get("audio_path")),
        "captions": bool(data.get("captions_path")),
        "images": bool(data.get("image_paths")) or (workdir / "images" / "visuals.json").exists(),
        "render": bool(data.get("video_path")),
    }


def _failure(data: dict) -> tuple[str | None, str | None]:
    if data.get("status") != "failed":
        return None, None
    for line in reversed(data.get("log") or []):
        m = _FAIL_RE.search(line)
        if m and m.group(1) in STAGE_ORDER:
            return m.group(1), m.group(2).strip()
    return None, (data.get("log") or [None])[-1]


def _discover(output_dir: Path) -> list[tuple[Path, bool]]:
    found: list[tuple[Path, bool]] = []
    for p in sorted(output_dir.glob("*/job.json")):
        found.append((p.parent, (p.parent / DRAFT_MARKER).exists()))
    for p in sorted((output_dir / "drafts").glob("*/job.json")):
        found.append((p.parent, (p.parent / DRAFT_MARKER).exists()))
    return found


# ------------------------------------------------------------------ per-table builders
def _stage_rows(data: dict, workdir: Path, draft: bool, current: str | None,
                fail_stage: str | None, fail_err: str | None) -> list[tuple]:
    produced = _produced(data, workdir, draft)
    rows = []
    for stage in STAGE_ORDER:
        cache = _read_json(workdir / ".cache" / f"{stage}.json")
        cache = cache if isinstance(cache, dict) else None
        ended = None
        if cache:
            f = workdir / ".cache" / f"{stage}.json"
            ended = _ts(cache.get("written_at"), f.stat().st_mtime)
        if draft and stage == "fact_check" and not data.get("fact_check"):
            rows.append((stage, None, "skipped", None, "draft: fact_check skipped", ended))
        elif stage == (fail_stage or (current if data.get("status") == "failed" else None)):
            rows.append((stage, (cache or {}).get("version"), "failed", None, fail_err, ended))
        elif produced[stage]:
            rows.append((stage, (cache or {}).get("version"), "ok" if cache else "cached",
                         (cache or {}).get("seconds"), None, ended))
    return rows


def _gate_rows(data: dict, workdir: Path, now: float) -> list[tuple]:
    rows = []
    fc = data.get("fact_check")
    if not isinstance(fc, dict):
        fc = _read_json(workdir / "fact_check.json")
    if isinstance(fc, dict) and "passed" in fc:
        rows.append(("fact_check", int(bool(fc["passed"])), json.dumps(fc.get("issues") or []),
                     now))
    sq = _read_json(workdir / "script_quality.json")
    if isinstance(sq, list) and sq:
        last = sq[-1] if isinstance(sq[-1], dict) else {}
        rows.append(("policy", int(bool(last.get("passed"))),
                     json.dumps(last.get("issues") or []), now))
    elif isinstance(sq, dict) and "passed" in sq:
        rows.append(("policy", int(bool(sq["passed"])), json.dumps(sq.get("issues") or []), now))
    return rows


def _asset_rows(workdir: Path) -> list[tuple]:
    rows: list[tuple] = []
    vis = _read_json(workdir / "images" / "visuals.json")
    beats = vis.get("beats") if isinstance(vis, dict) else None
    for b in beats or []:
        if not isinstance(b, dict):
            continue
        v = b.get("visual") or {}
        s = v.get("source") if isinstance(v.get("source"), dict) else {}
        rows.append((
            b.get("beat"), v.get("kind"), s.get("provider") or v.get("engine"),
            None if s.get("id") is None else str(s.get("id")), s.get("creator"),
            s.get("license"), s.get("source_url"), v.get("path"),
        ))
    if not rows:  # fallback: attribution.txt, one credit per line
        att = workdir / "images" / "attribution.txt"
        try:
            lines = [ln.strip() for ln in att.read_text(encoding="utf-8").splitlines()]
        except OSError:
            lines = []
        for i, ln in enumerate(x for x in lines if x):
            m = re.search(r"(Video|Photo|Image) by (.+?) on (\w+)(?: \(id (\S+?)\))?", ln)
            url = re.search(r"https?://\S+", ln)
            rows.append((i, m.group(1).lower() if m else None, m.group(3) if m else None,
                         m.group(4) if m else None, m.group(2) if m else None, None,
                         url.group(0) if url else None, None))
    return rows


def _ledger_publishes(ledger: Path) -> tuple[list[dict], list[dict]]:
    posts: list[dict] = []
    yts: list[dict] = []
    if not ledger.exists():
        return posts, yts
    try:
        con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return posts, yts
    try:
        for table, out in (("publish_posts", posts), ("youtube_uploads", yts)):
            try:
                out.extend(dict(r) for r in con.execute(f"SELECT * FROM {table}"))
            except sqlite3.Error:
                continue
    finally:
        con.close()
    return posts, yts


# ------------------------------------------------------------------ public API
def sync_from_jobs(output_dir: str | Path = "output",
                   db: str | Path | None = None) -> dict[str, int]:
    """Mirror every job dir into the tracking DB. Idempotent; job.json wins."""
    out = Path(output_dir)
    counts = {"jobs": 0, "drafts": 0, "stage_runs": 0, "gate_results": 0, "assets": 0,
              "publishes": 0, "skipped": 0}
    now = time.time()
    con = schema.connect(db)
    try:
        known: dict[str, str | None] = {}  # job_id -> content_hash
        with con:
            for workdir, draft in _discover(out):
                data = _read_json(workdir / "job.json")
                if not isinstance(data, dict) or not data.get("job_id"):
                    counts["skipped"] += 1
                    continue
                jid = data["job_id"]
                known[jid] = data.get("content_hash")
                produced = _produced(data, workdir, draft)
                current = next((s for s in STAGE_ORDER if not produced[s]), None)
                fail_stage, fail_err = _failure(data)
                if data.get("status") == "failed" and fail_stage:
                    current = fail_stage
                created = _ts(data.get("created_at"))
                mtime = (workdir / "job.json").stat().st_mtime
                con.execute(
                    """INSERT INTO jobs (job_id, channel, format, topic, status, current_stage,
                         error, workdir, video_path, content_hash, approved_hash, approved_by,
                         approval_valid, is_draft, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(job_id) DO UPDATE SET channel=excluded.channel,
                         format=excluded.format, topic=excluded.topic, status=excluded.status,
                         current_stage=excluded.current_stage, error=excluded.error,
                         workdir=excluded.workdir, video_path=excluded.video_path,
                         content_hash=excluded.content_hash,
                         approved_hash=excluded.approved_hash,
                         approved_by=excluded.approved_by,
                         approval_valid=excluded.approval_valid, is_draft=excluded.is_draft,
                         created_at=excluded.created_at, updated_at=excluded.updated_at""",
                    (jid, data.get("channel"), data.get("format"), data.get("topic"),
                     data.get("status"), current, fail_err, str(workdir),
                     data.get("video_path"), data.get("content_hash"), data.get("approved_hash"),
                     data.get("approved_by"), _approval_valid(data), int(draft), created, mtime),
                )
                for t in ("stage_runs", "gate_results", "assets", "publishes"):
                    con.execute(f"DELETE FROM {t} WHERE job_id = ?", (jid,))
                for r in _stage_rows(data, workdir, draft, current, fail_stage, fail_err):
                    con.execute("INSERT INTO stage_runs (job_id, stage, version, status, seconds,"
                                " error, ended_at) VALUES (?,?,?,?,?,?,?)", (jid, *r))
                    counts["stage_runs"] += 1
                for r in _gate_rows(data, workdir, now):
                    con.execute("INSERT INTO gate_results (job_id, gate, passed, issues_json,"
                                " created_at) VALUES (?,?,?,?,?)", (jid, *r))
                    counts["gate_results"] += 1
                for i, r in enumerate(_asset_rows(workdir)):
                    r = (i if r[0] is None else r[0], *r[1:])
                    con.execute("INSERT OR REPLACE INTO assets (job_id, beat, kind, source,"
                                " source_id, creator, licence, page_url, local_path)"
                                " VALUES (?,?,?,?,?,?,?,?,?)", (jid, *r))
                    counts["assets"] += 1
                counts["drafts" if draft else "jobs"] += 1

            posts, yts = _ledger_publishes(out / "ledger.sqlite")
            for p in posts:
                if p.get("job_id") not in known:
                    continue
                dry = str(p.get("remote_id") or "").startswith("DRY_RUN:")
                upd = _ts(p.get("updated_at"), now)
                con.execute(
                    """INSERT OR REPLACE INTO publishes (job_id, platform, target, idem_key,
                       remote_id, status, content_hash, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (p["job_id"], p.get("platform") or "", "buffer_dry_run" if dry else "buffer",
                     p.get("idem_key"), p.get("remote_id"), p.get("status"),
                     p.get("content_hash"), upd))
                counts["publishes"] += 1
            by_hash = {h: j for j, h in known.items() if h}
            for y in yts:
                jid = by_hash.get(y.get("content_hash"))
                if not jid:
                    continue
                status = {"uploaded": "posted"}.get(y.get("status"), y.get("status"))
                con.execute(
                    """INSERT OR REPLACE INTO publishes (job_id, platform, target, idem_key,
                       remote_id, status, content_hash, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (jid, "youtube", "youtube_private", y.get("content_hash"), y.get("video_id"),
                     status, y.get("content_hash"), _ts(y.get("updated_at"), now)))
                counts["publishes"] += 1
    finally:
        con.close()
    return counts


# ------------------------------------------------------------------ best-effort event helpers
def record_job_event(job_id: str, *, stage: str | None = None, status: str | None = None,
                     error: str | None = None, seconds: float | None = None,
                     db: str | Path | None = None, **fields: Any) -> bool:
    """Best-effort hook for the runner: never raises. Returns True when written.

    Only updates an existing ``jobs`` row (``track sync`` is the authority) and records one
    ``stage_runs`` row when ``stage`` and a valid ``status`` are given.
    """
    try:
        con = schema.connect(db)
        try:
            with con:
                upd = {"current_stage": stage, "error": error, "updated_at": time.time()}
                if fields.get("job_status"):
                    upd["status"] = fields["job_status"]
                sets = ", ".join(f"{k}=?" for k in upd)
                cur = con.execute(f"UPDATE jobs SET {sets} WHERE job_id=?",
                                  (*upd.values(), job_id))
                if cur.rowcount and stage and status in ("ok", "failed", "cached", "skipped"):
                    con.execute(
                        """INSERT INTO stage_runs (job_id, stage, status, seconds, error, ended_at)
                           VALUES (?,?,?,?,?,?) ON CONFLICT(job_id, stage) DO UPDATE SET
                           status=excluded.status, seconds=excluded.seconds,
                           error=excluded.error, ended_at=excluded.ended_at""",
                        (job_id, stage, status, seconds, error, time.time()))
            return bool(cur.rowcount)
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return False


def record_gate_result(job_id: str, gate: str, passed: bool, issues: list[str] | None = None,
                       db: str | Path | None = None) -> bool:
    """Best-effort gate recorder; never raises."""
    try:
        con = schema.connect(db)
        try:
            with con:
                exists = con.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if not exists:
                    return False
                con.execute(
                    """INSERT INTO gate_results (job_id, gate, passed, issues_json, created_at)
                       VALUES (?,?,?,?,?) ON CONFLICT(job_id, gate) DO UPDATE SET
                       passed=excluded.passed, issues_json=excluded.issues_json,
                       created_at=excluded.created_at""",
                    (job_id, gate, int(passed), json.dumps(issues or []), time.time()))
            return True
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ report
def build_report(db: str | Path | None = None) -> dict[str, Any]:
    con = schema.connect(db, readonly=True)
    try:
        q = lambda sql: [dict(r) for r in con.execute(sql)]
        return {
            "by_channel_status": q("SELECT channel, status, COUNT(*) AS n FROM jobs "
                                   "WHERE is_draft=0 GROUP BY channel, status ORDER BY channel"),
            "failed": q("SELECT job_id, channel, current_stage, error FROM jobs "
                        "WHERE status='failed'"),
            "approval_invalid": q("SELECT job_id, channel, approved_by FROM jobs WHERE "
                                  "status='approved' AND COALESCE(approval_valid,0)=0"),
            "drafts": q("SELECT job_id, channel, status, current_stage FROM jobs "
                        "WHERE is_draft=1"),
        }
    finally:
        con.close()
