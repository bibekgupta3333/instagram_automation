"""Buffer GraphQL publisher (docs 05 §4/§6, 16 §4-§5). Deterministic code, no LLM.

- ``DRY_RUN=1`` is the DEFAULT: payloads are logged and nothing is sent. Set ``DRY_RUN=0``
  and ``BUFFER_API_KEY`` to post for real.
- Every request passes ``gate.assert_publishable`` first.
- Idempotency: one key per (job_id, platform), reserved in SQLite BEFORE createPost. A retry
  never re-submits a key that is posted or whose outcome is unknown (``NeedsReconcile``).
- Rate limits: 100 requests / 15 min and a daily cap (250 Essentials, 500 Team), shared with
  the Buffer MCP and CLI (docs 05 §4). Tracked in the same SQLite file.
- Optional second gate inside Buffer: ``saveToDraft`` (Essentials) or ``needsApproval`` (Team).

UNVERIFIED (docs 05 §4/§10 mark these [?]; check in the Explorer at
developers.buffer.com/explorer.html before DRY_RUN=0):
- the GraphQL endpoint path (``https://api.buffer.com`` vs ``/graphql``), the input type name
  ``CreatePostInput``, and the result union members ``PostActionSuccess`` / ``MutationError``;
- enum literals: ``reel`` (instagram.type), ``customScheduled``/``addToQueue`` (mode),
  ``automatic`` (schedulingType), ``public``/``private`` (youtube.privacy);
- the exact ``metadata`` nesting, and whether YouTube ``privacy``/``categoryId`` take effect
  for Shorts; whether Facebook honours ``thumbnailUrl``; ``isAiGenerated`` on Facebook.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from studio.contracts import Platform, PublishRequest
from studio.publish.gate import assert_publishable, check_first_comment

log = logging.getLogger(__name__)

BUFFER_ENDPOINT_DEFAULT = "https://api.buffer.com"  # UNVERIFIED: path may need /graphql
BUFFER_PLATFORMS = (Platform.YT_SHORTS, Platform.IG_REELS, Platform.FB_REELS)
SecondGate = Literal["none", "draft", "approval"]

CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}
""".strip()


class BufferError(RuntimeError):
    pass


class RateLimited(BufferError):
    def __init__(self, msg: str, retry_after: float):
        super().__init__(msg)
        self.retry_after = retry_after


class BufferRejected(BufferError):
    """Definitive 4xx: the post was not created, so the key may be retried after a fix."""


class NeedsReconcile(BufferError):
    """A previous attempt for this key has an unknown outcome. Check Buffer (posts query /
    MCP list_posts) and mark it with ``PostLedger.resolve`` before retrying."""


def env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() not in {"0", "false", "no", "off"}


def idempotency_key(job_id: str, platform: str) -> str:
    return hashlib.sha256(f"{job_id}|{platform}".encode()).hexdigest()


class PostLedger:
    """SQLite table of Buffer posts + request log for rate limiting (shares output/ledger.sqlite)."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or os.environ.get("STUDIO_LEDGER_PATH") or "output/ledger.sqlite")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS publish_posts (
                    idem_key TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL, platform TEXT NOT NULL, content_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending','posted','failed')),
                    remote_id TEXT, error TEXT, updated_at TEXT NOT NULL)"""
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS buffer_requests (ts REAL NOT NULL, op TEXT)"
            )

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT idem_key, job_id, platform, content_hash, status, remote_id, error"
                " FROM publish_posts WHERE idem_key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        cols = ("idem_key", "job_id", "platform", "content_hash", "status", "remote_id", "error")
        return dict(zip(cols, row, strict=True))

    def reserve(self, key: str, job_id: str, platform: str, content_hash: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO publish_posts (idem_key, job_id, platform, content_hash, status,"
                " updated_at) VALUES (?,?,?,?, 'pending', ?)"
                " ON CONFLICT(idem_key) DO UPDATE SET status='pending', error=NULL,"
                " content_hash=excluded.content_hash, updated_at=excluded.updated_at"
                " WHERE publish_posts.status = 'failed'",
                (key, job_id, platform, content_hash, now),
            )

    def resolve(
        self, key: str, status: str, remote_id: str | None = None, error: str | None = None
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE publish_posts SET status=?, remote_id=?, error=?, updated_at=?"
                " WHERE idem_key=?",
                (status, remote_id, error, datetime.now(UTC).isoformat(), key),
            )

    def log_request(self, ts: float, op: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO buffer_requests (ts, op) VALUES (?,?)", (ts, op))

    def requests_since(self, ts: float) -> list[float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts FROM buffer_requests WHERE ts >= ? ORDER BY ts", (ts,)
            ).fetchall()
        return [r[0] for r in rows]


@dataclass
class PostResult:
    platform: str
    idem_key: str
    dry_run: bool
    status: Literal["dry_run", "posted", "skipped_duplicate"]
    remote_id: str | None
    payload: dict[str, Any]


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        log.warning("scheduled_at is naive; treating it as UTC")
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_create_post_input(
    request: PublishRequest,
    platform: Platform,
    *,
    channel_id: str,
    second_gate: SecondGate = "none",
    first_comment: str | None = None,
    youtube_privacy: str = "public",
    youtube_category_id: str | None = None,
) -> dict[str, Any]:
    """Build the ``CreatePostInput`` variables for one Buffer channel (one request per channel;
    docs 05 §4: no multi-channel fan-out)."""
    if platform not in BUFFER_PLATFORMS:
        raise BufferError(
            f"Buffer does not support {platform.value} (long-form YouTube goes "
            "through studio.publish.youtube)"
        )
    video: dict[str, Any] = {"url": request.media_url}
    # YouTube Shorts: no custom thumbnail via Buffer (docs 05 §3). FB thumbnailUrl: UNVERIFIED.
    if request.thumbnail_url and platform != Platform.YT_SHORTS:
        video["thumbnailUrl"] = request.thumbnail_url
    if platform == Platform.YT_SHORTS:
        video["metadata"] = {"title": request.title[:100]}

    inp: dict[str, Any] = {
        "channelId": channel_id,
        "text": request.caption_by_platform[platform.value],
        "schedulingType": "automatic",
        "assets": [{"video": video}],
        "needsApproval": second_gate == "approval",  # required boolean [V]
    }
    if second_gate == "draft":
        inp["saveToDraft"] = True
    if request.scheduled_at is not None:
        inp["mode"] = "customScheduled"
        inp["dueAt"] = _utc_iso(request.scheduled_at)
    else:
        inp["mode"] = "addToQueue"

    if platform == Platform.YT_SHORTS:
        yt: dict[str, Any] = {
            "title": request.title[:100],
            "privacy": youtube_privacy,  # UNVERIFIED enum literal; may be ignored for Shorts
            "madeForKids": False,
            "isAiGenerated": bool(request.ai_disclosure),
            "notifySubscribers": True,
        }
        if youtube_category_id:
            yt["categoryId"] = youtube_category_id
        inp["metadata"] = {"youtube": yt}
    elif platform == Platform.IG_REELS:
        ig: dict[str, Any] = {
            "type": "reel",  # UNVERIFIED enum literal
            "shouldShareToFeed": True,
            "isAiGenerated": bool(request.ai_disclosure),
        }
        if first_comment:
            ig["firstComment"] = first_comment
        inp["metadata"] = {"instagram": ig}
    elif platform == Platform.FB_REELS:
        fb: dict[str, Any] = {"title": request.title}
        if first_comment:
            fb["firstComment"] = first_comment
        # isAiGenerated on Facebook is [?] in docs 05: not sent. A 9:16 upload becomes a Reel.
        inp["metadata"] = {"facebook": fb}
    return inp


class BufferPublisher:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        channel_map: dict[str, str] | None = None,
        dry_run: bool | None = None,
        second_gate: SecondGate | None = None,
        ledger: PostLedger | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        window_limit: int = 100,
        window_seconds: float = 15 * 60,
        daily_limit: int | None = None,
        max_429_retries: int = 2,
    ):
        e = os.environ
        self.dry_run = env_flag("DRY_RUN", True) if dry_run is None else dry_run
        self.api_key = api_key if api_key is not None else e.get("BUFFER_API_KEY", "")
        self.endpoint = endpoint or e.get("BUFFER_API_URL") or BUFFER_ENDPOINT_DEFAULT
        self.channel_map = channel_map if channel_map is not None else load_channel_map()
        self.second_gate: SecondGate = second_gate or e.get("BUFFER_SECOND_GATE", "draft")  # type: ignore[assignment]
        if self.second_gate not in ("none", "draft", "approval"):
            raise ValueError(
                f"BUFFER_SECOND_GATE must be none|draft|approval, got {self.second_gate!r}"
            )
        self.ledger = ledger or PostLedger()
        self.clock = clock
        self.sleep = sleep
        self.window_limit = window_limit
        self.window_seconds = window_seconds
        self.daily_limit = daily_limit or int(e.get("BUFFER_DAILY_LIMIT", "250"))
        self.max_429_retries = max_429_retries
        self._transport = transport
        self._http: httpx.Client | None = None
        if not self.dry_run and not self.api_key:
            raise BufferError("DRY_RUN=0 requires BUFFER_API_KEY")

    # -- helpers ----------------------------------------------------------------------------
    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=60.0,
                transport=self._transport,
            )
        return self._http

    def channel_id(self, channel: str, platform: Platform) -> str:
        key = f"{channel}:{platform.value}"
        if key in self.channel_map:
            return self.channel_map[key]
        if self.dry_run:
            return f"DRY_RUN:{key}"
        raise BufferError(f"no Buffer channel id for {key!r} (BUFFER_CHANNEL_MAP)")

    def _check_rate(self) -> None:
        now = self.clock()
        recent = self.ledger.requests_since(now - 24 * 3600)
        window = [t for t in recent if t >= now - self.window_seconds]
        if len(window) >= self.window_limit:
            wait = window[0] + self.window_seconds - now
            raise RateLimited(f"{self.window_limit} requests / 15 min reached", max(1.0, wait))
        if len(recent) >= self.daily_limit:
            wait = recent[0] + 24 * 3600 - now
            raise RateLimited(f"daily Buffer budget {self.daily_limit} reached", max(1.0, wait))

    def _graphql(self, query: str, variables: dict[str, Any], op: str) -> dict[str, Any]:
        for attempt in range(self.max_429_retries + 1):
            self._check_rate()
            self.ledger.log_request(self.clock(), op)
            resp = self.http.post(self.endpoint, json={"query": query, "variables": variables})
            if resp.status_code == 429:  # rejected, so retrying cannot double-post
                ra = float(resp.headers.get("retry-after") or 60)
                if attempt == self.max_429_retries:
                    raise RateLimited("Buffer returned 429", ra)
                self.sleep(ra)
                continue
            if 400 <= resp.status_code < 500:
                raise BufferRejected(f"Buffer HTTP {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()  # 5xx: outcome unknown, caller leaves the key pending
            return resp.json()
        raise RateLimited("Buffer returned 429", 60.0)  # pragma: no cover

    # -- publish ----------------------------------------------------------------------------
    def publish(
        self,
        request: PublishRequest,
        *,
        first_comments: dict[str, str] | None = None,
    ) -> list[PostResult]:
        """Gate, then createPost once per Buffer-supported platform in the request."""
        assert_publishable(request)
        results: list[PostResult] = []
        for p in request.platforms:
            platform = Platform(p)
            if platform not in BUFFER_PLATFORMS:
                log.info("skipping %s: not a Buffer platform", platform.value)
                continue
            fc = (first_comments or {}).get(platform.value)
            check_first_comment(platform.value, fc)
            results.append(self._publish_one(request, platform, fc))
        return results

    def _publish_one(
        self, request: PublishRequest, platform: Platform, first_comment: str | None
    ) -> PostResult:
        key = idempotency_key(request.job_id, platform.value)
        inp = build_create_post_input(
            request,
            platform,
            channel_id=self.channel_id(request.channel, platform),
            second_gate=self.second_gate,
            first_comment=first_comment,
        )
        payload = {"query": CREATE_POST_MUTATION, "variables": {"input": inp}}

        if self.dry_run:
            log.info(
                "DRY_RUN Buffer createPost %s %s:\n%s",
                request.job_id,
                platform.value,
                json.dumps(payload, indent=2),
            )
            return PostResult(platform.value, key, True, "dry_run", None, payload)

        existing = self.ledger.get(key)
        if existing and existing["status"] == "posted":
            log.info("idempotent skip: %s already posted as %s", key[:12], existing["remote_id"])
            return PostResult(
                platform.value, key, False, "skipped_duplicate", existing["remote_id"], payload
            )
        if existing and existing["status"] == "pending":
            raise NeedsReconcile(
                f"{request.job_id}/{platform.value}: earlier attempt has unknown outcome; "
                "check Buffer before retrying"
            )
        self.ledger.reserve(key, request.job_id, platform.value, request.content_hash)
        try:
            body = self._graphql(CREATE_POST_MUTATION, payload["variables"], "createPost")
        except (RateLimited, BufferRejected) as exc:
            self.ledger.resolve(key, "failed", error=str(exc))  # definitively not created
            raise
        # Timeouts / 5xx propagate and leave the key 'pending' -> NeedsReconcile on retry.
        if body.get("errors"):
            msg = json.dumps(body["errors"])[:500]
            self.ledger.resolve(key, "failed", error=msg)
            raise BufferError(f"GraphQL errors: {msg}")
        res = (body.get("data") or {}).get("createPost") or {}
        post = res.get("post")
        if not post or not post.get("id"):
            msg = res.get("message") or f"unexpected createPost result: {res}"
            self.ledger.resolve(key, "failed", error=msg)
            raise BufferError(f"Buffer MutationError: {msg}")
        self.ledger.resolve(key, "posted", remote_id=post["id"])
        return PostResult(platform.value, key, False, "posted", post["id"], payload)


def load_channel_map() -> dict[str, str]:
    """``BUFFER_CHANNEL_MAP``: path to a JSON/YAML file {"<channel>:<platform>": "<buffer id>"}."""
    path = os.environ.get("BUFFER_CHANNEL_MAP")
    if not path:
        return {}
    text = Path(path).read_text()
    if path.endswith((".yaml", ".yml")):
        import yaml

        return {str(k): str(v) for k, v in (yaml.safe_load(text) or {}).items()}
    return {str(k): str(v) for k, v in json.loads(text).items()}
