"""Trend/topic intake (doc 37 §4). Every fetched string is untrusted data: HTML and control
characters are stripped, lengths are capped, and nothing here is ever interpreted as an
instruction. Fetch failures return [] with a logged warning and never raise."""

from __future__ import annotations

import csv
import html
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from studio.pipeline.pipeline_schema import connect

log = logging.getLogger(__name__)

TRENDS_URL = "https://trends.google.com/trending/rss"
USER_AGENT = "instagram-automation-studio/0.1 (+local research tool; read-only trend intake)"
MAX_TERM = 120
MAX_TITLE = 200
MAX_URL = 500
MAX_BYTES = 2_000_000
MAX_ITEMS = 100
CACHE_DIR = Path("output/.cache/trends")
LIBRARY_CSV = Path(__file__).resolve().parents[2] / "docs" / "6-script-library" / "catalog.csv"

_TAG = re.compile(r"<[^>]*>")
_WS = re.compile(r"\s+")


def clean_text(value: Any, limit: int = MAX_TERM) -> str:
    """Untrusted text -> single-line plain text: unescape, strip tags and control chars, cap."""
    s = html.unescape(str(value or ""))
    s = _TAG.sub(" ", s)
    s = "".join(" " if unicodedata.category(c)[0] == "C" else c for c in s)
    return _WS.sub(" ", s).strip()[:limit]


def clean_url(value: Any) -> str:
    u = clean_text(value, MAX_URL)
    return u if u.lower().startswith(("http://", "https://")) and " " not in u else ""


@dataclass
class RawTrend:
    term: str
    source: str
    region: str = "US"
    volume: int | None = None
    news: list[dict] = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)


class Source(Protocol):
    def fetch(self) -> list[RawTrend]: ...


def parse_traffic(text: str | None) -> int | None:
    """'2000+' / '200,000+' / '2M+' -> int."""
    m = re.match(r"\s*([\d,.]+)\s*([KMB]?)\+?\s*$", text or "", re.IGNORECASE)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()])


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_rss(data: bytes | str, *, region: str = "US", fetched_at: float | None = None) -> list[RawTrend]:
    """Parse the Google Trends RSS (``ht:`` namespace matched by local name). Returns [] on
    malformed XML or any DTD/entity declaration (billion-laughs guard)."""
    raw = data.encode() if isinstance(data, str) else data
    if len(raw) > MAX_BYTES or re.search(rb"<!(?:DOCTYPE|ENTITY)", raw, re.IGNORECASE):
        log.warning("trends RSS rejected: too large or contains DTD/entity declarations")
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        log.warning("trends RSS is not valid XML: %s", exc)
        return []
    now = fetched_at if fetched_at is not None else time.time()
    out: list[RawTrend] = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        fields: dict[str, str] = {}
        news: list[dict] = []
        for ch in item:
            name = _local(ch.tag)
            if name == "news_item":
                sub = {_local(c.tag): (c.text or "") for c in ch}
                title = clean_text(sub.get("news_item_title"), MAX_TITLE)
                if title and len(news) < 5:
                    news.append({"title": title, "url": clean_url(sub.get("news_item_url")),
                                 "source": clean_text(sub.get("news_item_source"), 80)})
            else:
                fields.setdefault(name, ch.text or "")
        term = clean_text(fields.get("title"), MAX_TERM)
        if not term:
            continue
        published = None
        if fields.get("pubdate"):
            try:
                published = parsedate_to_datetime(fields["pubdate"].strip()).timestamp()
            except (TypeError, ValueError):
                published = None
        out.append(RawTrend(
            term=term, source="trends", region=region, volume=parse_traffic(fields.get("approx_traffic")),
            news=news, fetched_at=now,
            raw={"approx_traffic": clean_text(fields.get("approx_traffic"), 30), "published": published}))
        if len(out) >= MAX_ITEMS:
            break
    return out


class GoogleTrendsRSS:
    def __init__(self, region: str = "US", *, cache_dir: Path | str | None = None,
                 ttl_minutes: float | None = None, transport: httpx.BaseTransport | None = None,
                 timeout: float = 15.0):
        self.region = re.sub(r"[^A-Za-z-]", "", region or "US").upper()[:8] or "US"
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self.ttl = (ttl_minutes if ttl_minutes is not None
                    else float(os.environ.get("STUDIO_TRENDS_TTL_MIN", "60"))) * 60
        self.transport = transport
        self.timeout = timeout

    @property
    def cache_file(self) -> Path:
        return self.cache_dir / f"trends_{self.region}.xml"

    def _cached(self) -> bytes | None:
        try:
            p = self.cache_file
            if p.exists() and time.time() - p.stat().st_mtime < self.ttl:
                return p.read_bytes()
        except OSError:
            pass
        return None

    def fetch(self) -> list[RawTrend]:
        body = self._cached()
        if body is None:
            try:
                with httpx.Client(timeout=self.timeout, transport=self.transport,
                                  headers={"User-Agent": USER_AGENT}, follow_redirects=False) as c:
                    resp = c.get(TRENDS_URL, params={"geo": self.region})
                    resp.raise_for_status()
                    body = resp.content
            except Exception as exc:  # noqa: BLE001 network, status, anything: never raise
                log.warning("google trends fetch failed: %s", type(exc).__name__)
                return []
            items = parse_rss(body, region=self.region)
            if items:  # only cache a feed we could parse
                try:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    self.cache_file.write_bytes(body)
                except OSError as exc:
                    log.warning("could not write trends cache: %s", exc)
            return items
        return parse_rss(body, region=self.region)


class FileSource:
    """One topic per line; blank lines and ``#`` comments ignored."""

    def __init__(self, path: Path | str, region: str = "US"):
        self.path = Path(path)
        self.region = region

    def fetch(self) -> list[RawTrend]:
        try:
            lines = self.path.read_text(errors="replace").splitlines()
        except OSError as exc:
            log.warning("topics file unreadable: %s", exc)
            return []
        now = time.time()
        out = []
        for ln in lines:
            ln = ln.split(" #", 1)[0] if " #" in ln else ln
            term = clean_text(ln, MAX_TERM)
            if term and not term.startswith("#"):
                out.append(RawTrend(term=term, source="file", region=self.region, fetched_at=now,
                                    raw={"path": self.path.name}))
        return out


class LibrarySource:
    """docs/6-script-library/catalog.csv: episode_id, channel (display name), title, ..."""

    def __init__(self, path: Path | str | None = None, brands: dict | None = None):
        self.path = Path(path) if path else LIBRARY_CSV
        self.brands = brands

    def fetch(self) -> list[RawTrend]:
        if self.brands is None:
            from studio.config import load_brands
            self.brands = load_brands()
        by_name = {b.name.strip().lower(): slug for slug, b in self.brands.items()}
        try:
            with self.path.open(newline="", errors="replace") as f:
                rows = list(csv.DictReader(f))
        except OSError as exc:
            log.warning("library catalog unreadable: %s", exc)
            return []
        now = time.time()
        out = []
        for r in rows:
            term = clean_text(r.get("title"), MAX_TERM)
            if not term:
                continue
            slug = by_name.get(clean_text(r.get("channel"), 80).lower())
            out.append(RawTrend(term=term, source="library", fetched_at=now,
                                raw={"episode_id": clean_text(r.get("episode_id"), 20), "channel": slug}))
        return out


def save_candidates(rows: list[RawTrend], db: sqlite3.Connection | str | os.PathLike | None = None) -> int:
    """Insert into trend_candidates (UNIQUE term/source/day, so re-runs are no-ops). Returns the
    number of new rows."""
    own = not isinstance(db, sqlite3.Connection)
    con = db if isinstance(db, sqlite3.Connection) else connect(db)
    try:
        before = con.total_changes
        for r in rows:
            term = clean_text(r.term, MAX_TERM)
            if not term:
                continue
            day = datetime.fromtimestamp(r.fetched_at).strftime("%Y-%m-%d")
            payload = {"news": r.news, **{k: v for k, v in r.raw.items()}}
            con.execute(
                "INSERT OR IGNORE INTO trend_candidates(term, source, region, volume, raw_json, fetched_at, day)"
                " VALUES (?,?,?,?,?,?,?)",
                (term, r.source, r.region, r.volume, json.dumps(payload, default=str), r.fetched_at, day))
        con.commit()
        return con.total_changes - before
    finally:
        if own:
            con.close()
