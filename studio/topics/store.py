"""Persist triage results into the shared ``topics`` table (doc 37 §6)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from studio.topics.triage import TriageResult


def topic_hash(channel: str, fmt: str, topic: str) -> str:
    """Same digest input as studio.pipeline.runner.job_id_for (full hex, not truncated)."""
    return hashlib.sha256(f"{channel}|{fmt}|{topic.strip()}".encode()).hexdigest()


def pending_candidates(con: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    q = ("SELECT c.id, c.term, c.source, c.volume, c.raw_json FROM trend_candidates c "
         "WHERE NOT EXISTS (SELECT 1 FROM topics t WHERE t.candidate_id = c.id) "
         "ORDER BY COALESCE(c.volume, 0) DESC, c.id")
    rows = con.execute(q + (" LIMIT ?" if limit else ""), (limit,) if limit else ()).fetchall()
    out = []
    for r in rows:
        try:
            raw = json.loads(r["raw_json"] or "{}")
        except ValueError:
            raw = {}
        out.append({"id": r["id"], "term": r["term"], "source": r["source"], "volume": r["volume"],
                    "raw": raw, "news": raw.get("news", [])})
    return out


def existing_topic_texts(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT topic_text FROM topics WHERE status != 'rejected'")]


def save_triage(con: sqlite3.Connection, candidate_id: int | None, res: TriageResult,
                fmt: str = "short") -> bool:
    """Write one topics row. Accepted -> status proposed, topic_text = the angle. Rejected ->
    status rejected with a placeholder topic_text (the raw trend term lives only in
    safety_json.source_term). Idempotent via topic_hash UNIQUE. Returns True if a row was added."""
    if res.accepted and res.angle and res.channel:
        text, h, status = res.angle.strip(), topic_hash(res.channel, fmt, res.angle), "proposed"
    else:
        text = f"[rejected:{res.klass}]"
        h = hashlib.sha256(f"rejected|{candidate_id}|{res.reject_reason}".encode()).hexdigest()
        status = "rejected"
    before = con.total_changes
    con.execute(
        "INSERT OR IGNORE INTO topics(candidate_id, channel, format, topic_text, topic_hash, class,"
        " angle, status, reject_reason, safety_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (candidate_id, res.channel, fmt, text, h, res.klass, res.angle if res.accepted else None,
         status, res.reject_reason, json.dumps(res.safety, default=str), time.time()))
    con.commit()
    return con.total_changes > before
