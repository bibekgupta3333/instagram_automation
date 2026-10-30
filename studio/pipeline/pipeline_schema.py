"""Shared SQLite schema for the trend/tracking pipeline (doc 37 §6).

Single source of truth for table definitions; tracking, topics and the dashboard all use
``connect()``. The DB is an index and audit log: ``output/<job>/job.json`` stays the source of
truth and ``studio track sync`` rebuilds it. Override the path with ``STUDIO_PIPELINE_DB``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB = Path("output/pipeline.sqlite")

DDL = """
CREATE TABLE IF NOT EXISTS trend_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  term TEXT NOT NULL,
  source TEXT NOT NULL,
  region TEXT NOT NULL DEFAULT 'US',
  volume INTEGER,
  raw_json TEXT,
  fetched_at REAL NOT NULL,
  day TEXT NOT NULL,
  UNIQUE(term, source, day)
);
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER REFERENCES trend_candidates(id),
  channel TEXT,
  format TEXT NOT NULL DEFAULT 'short',
  topic_text TEXT NOT NULL,
  topic_hash TEXT NOT NULL UNIQUE,
  class TEXT,
  angle TEXT,
  status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed','approved_for_batch','rejected','queued','done')),
  reject_reason TEXT,
  safety_json TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  config_path TEXT,
  config_sha256 TEXT,
  daily_cap INTEGER,
  started_at REAL,
  finished_at REAL,
  mock INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'planned'
);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  batch_id INTEGER REFERENCES batches(id),
  topic_id INTEGER REFERENCES topics(id),
  channel TEXT,
  format TEXT,
  topic TEXT,
  status TEXT,
  current_stage TEXT,
  error TEXT,
  workdir TEXT,
  video_path TEXT,
  content_hash TEXT,
  approved_hash TEXT,
  approved_by TEXT,
  approval_valid INTEGER,
  is_draft INTEGER NOT NULL DEFAULT 0,
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS stage_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  stage TEXT NOT NULL,
  version INTEGER,
  status TEXT NOT NULL CHECK (status IN ('ok','failed','cached','skipped')),
  seconds REAL,
  error TEXT,
  ended_at REAL,
  UNIQUE(job_id, stage)
);
CREATE TABLE IF NOT EXISTS gate_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  gate TEXT NOT NULL CHECK (gate IN ('topic_triage','policy','fact_check','publish_gate')),
  passed INTEGER NOT NULL,
  issues_json TEXT,
  created_at REAL,
  UNIQUE(job_id, gate)
);
CREATE TABLE IF NOT EXISTS publishes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  platform TEXT NOT NULL,
  target TEXT NOT NULL CHECK (target IN ('buffer_dry_run','buffer','youtube_private')),
  idem_key TEXT,
  remote_id TEXT,
  status TEXT,
  content_hash TEXT,
  updated_at REAL,
  UNIQUE(job_id, platform, target)
);
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  beat INTEGER,
  kind TEXT,
  source TEXT,
  source_id TEXT,
  creator TEXT,
  licence TEXT,
  page_url TEXT,
  local_path TEXT,
  UNIQUE(job_id, beat)
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  params_json TEXT,
  actor TEXT,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','ok','failed','refused','stopped')),
  refused_reason TEXT,
  argv_json TEXT,
  log_path TEXT,
  exit_code INTEGER,
  created_at REAL NOT NULL,
  started_at REAL,
  ended_at REAL
);
CREATE TABLE IF NOT EXISTS approval_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('approve','reject','bulk_approve')),
  approver TEXT NOT NULL,
  content_hash TEXT,
  sample_json TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_channel ON jobs(channel);
CREATE INDEX IF NOT EXISTS topics_status ON topics(status);
"""


def db_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.environ.get("STUDIO_PIPELINE_DB") or DEFAULT_DB)


def connect(path: str | os.PathLike | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    """Open (and for writers, create) the pipeline DB. WAL, short transactions."""
    p = db_path(path)
    if readonly:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(p, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(DDL)
    con.row_factory = sqlite3.Row
    return con
