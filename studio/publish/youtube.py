"""Direct YouTube Data API v3 uploader: always PRIVATE; a human clicks Publish (docs 05 §1/§9, 26).

User decision (2026-10-23): videos are uploaded **private** to the user's channel with title,
description, tags, thumbnail and an accurate made-for-kids flag. The user reviews in YouTube
Studio and publishes there. Code never makes a video public or unlisted: ``build_video_body``
rejects any other privacy, and ``upload`` re-checks the privacy YouTube reports back.

Optional scheduling: ``publish_at`` sets ``status.publishAt``. YouTube only accepts it on a
private, never-published video, and YouTube itself flips it to public at that time. It is only
set when the user passes ``--schedule`` to ``scripts/youtube_upload.py``; the default is none.

Verified against developers.google.com/youtube/v3 on 2026-10-23:
- ``videos.insert``: part ``snippet,status``; media ``video/*``, max 256 GB; costs 1 unit in its
  own "Video Uploads" quota bucket (100 calls/day by default), separate from the 10,000-unit
  bucket. ``thumbnails.set`` ~50 units (``image/jpeg``/``image/png``, max 50 MB);
  ``playlistItems.insert`` 50 units. The older "1,600 units per upload" figure is outdated.
- ``status.privacyStatus`` (private|public|unlisted; **defaults to public if omitted**, so it is
  always sent), ``status.publishAt`` (only if private and never published),
  ``status.selfDeclaredMadeForKids``, ``status.containsSyntheticMedia`` (A/S disclosure),
  ``status.embeddable``.
- ``snippet.title`` ≤100 characters, ``snippet.description`` ≤5000 **bytes**, neither may contain
  ``<`` or ``>``; ``snippet.tags`` ≤500 characters total, commas count, and a tag with a space
  counts 2 extra for implied quotes; ``snippet.categoryId``; ``snippet.defaultLanguage``,
  ``snippet.defaultAudioLanguage``.
- Scopes: ``youtube.upload`` covers ``videos.insert`` and ``thumbnails.set``;
  ``playlistItems.insert`` needs ``youtube`` (or ``youtube.force-ssl``), so it is requested only
  when the user authorises playlists (``auth --playlists``).
- The videos.insert page now says unverified projects are "not restricted to private viewing
  mode" (older docs forced private until the compliance audit). Irrelevant here: we are
  private-only by decision either way.

OAuth: installed-app (Desktop) loopback flow with the client-secrets JSON at
``YOUTUBE_CLIENT_SECRETS_PATH``; the token is cached at ``~/.config/studio/youtube-token.json``
(override ``YOUTUBE_TOKEN_PATH``) with mode 0600. Token contents are never printed or logged.

Idempotency: one row per content_hash in ``youtube_uploads`` in the publish ledger
(``output/ledger.sqlite`` / ``STUDIO_LEDGER_PATH``). It is reserved BEFORE ``videos.insert``; an
interrupted upload leaves it ``pending`` and a retry raises ``NeedsReconcile`` (check YouTube
Studio, then ``YouTubeLedger.resolve``) instead of uploading a duplicate.

Needs ``google-api-python-client`` and ``google-auth-oauthlib`` (lazy imports; proposed for an
optional ``youtube`` extra in pyproject).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from studio.contracts import BrandPack, Platform, PublishRequest
from studio.publish.gate import DIRECT_PRIVACY, YOUTUBE_PLATFORMS, assert_publishable

log = logging.getLogger(__name__)

SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_YOUTUBE = "https://www.googleapis.com/auth/youtube"  # playlistItems.insert only
DEFAULT_TOKEN_PATH = Path("~/.config/studio/youtube-token.json")
ALLOWED_CATEGORIES = {"24": "Entertainment", "27": "Education"}
DEFAULT_CATEGORY_ID = "24"
DEFAULT_LANGUAGE = "en-GB"
TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX = 500
THUMBNAIL_MAX_BYTES = 50 * 1024 * 1024
THUMBNAIL_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
QUOTA = {"videos.insert": "1 (Video Uploads bucket, 100/day)", "thumbnails.set": 50,
         "playlistItems.insert": 50}


class PrivacyViolation(ValueError):
    """Anything other than a private upload was requested or reported."""


class MetaInvalid(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("invalid YouTube metadata: " + "; ".join(errors))
        self.errors = errors


class NeedsReconcile(RuntimeError):
    """A previous upload for this content has an unknown outcome. Check YouTube Studio, then
    ``YouTubeLedger.resolve(content_hash, 'uploaded', video_id=...)`` or ``'failed'``."""


def google_libs_available() -> bool:
    return all(
        importlib.util.find_spec(m) is not None for m in ("googleapiclient", "google_auth_oauthlib")
    )


def _require_libs() -> None:
    if not google_libs_available():
        raise NotImplementedError(
            "Direct YouTube upload needs google-api-python-client and google-auth-oauthlib "
            "(not installed; see infra/YOUTUBE.md). Uploads are always private."
        )


# ---------------------------------------------------------------- metadata + request body


@dataclass
class UploadMeta:
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    category_id: str = DEFAULT_CATEGORY_ID
    thumbnail: Path | None = None
    playlist_id: str | None = None
    made_for_kids: bool = False
    contains_synthetic_media: bool = True
    publish_at: datetime | None = None
    default_language: str = DEFAULT_LANGUAGE
    default_audio_language: str = DEFAULT_LANGUAGE
    privacy: str = DIRECT_PRIVACY

    def canonical(self) -> dict[str, Any]:
        """Stable JSON form (paths as strings) used for hashing and dry-run output."""
        d = asdict(self)
        d["thumbnail"] = str(self.thumbnail) if self.thumbnail else None
        d["publish_at"] = self.publish_at.isoformat() if self.publish_at else None
        return d


def tags_length(tags: list[str]) -> int:
    """Length as YouTube counts it: commas between tags count, and a tag containing a space
    counts 2 extra characters for the implied quotation marks."""
    if not tags:
        return 0
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + len(tags) - 1


def meta_problems(meta: UploadMeta, *, now: datetime | None = None) -> list[str]:
    errs: list[str] = []
    if meta.privacy != DIRECT_PRIVACY:
        errs.append(f"privacy must be 'private' (got {meta.privacy!r}); the user clicks Publish")
    title = meta.title.strip()
    if not title:
        errs.append("title is empty")
    elif len(meta.title) > TITLE_MAX:
        errs.append(f"title is {len(meta.title)} characters (max {TITLE_MAX})")
    n = len(meta.description.encode("utf-8"))
    if n > DESCRIPTION_MAX_BYTES:
        errs.append(f"description is {n} bytes (max {DESCRIPTION_MAX_BYTES})")
    for label, text in (("title", meta.title), ("description", meta.description)):
        if "<" in text or ">" in text:
            errs.append(f"{label} may not contain '<' or '>'")
    if any(not t.strip() or "," in t or "<" in t or ">" in t for t in meta.tags):
        errs.append("tags must be non-empty and contain no ',', '<' or '>'")
    total = tags_length(meta.tags)
    if total > TAGS_MAX:
        errs.append(f"tags total {total} characters (max {TAGS_MAX})")
    if meta.category_id not in ALLOWED_CATEGORIES:
        errs.append(f"categoryId must be one of {sorted(ALLOWED_CATEGORIES)} "
                    f"(got {meta.category_id!r})")
    if meta.thumbnail is not None:
        suffix = meta.thumbnail.suffix.lower()
        if suffix not in THUMBNAIL_MIME:
            errs.append(f"thumbnail must be .jpg or .png (got {meta.thumbnail.name})")
        if not meta.thumbnail.is_file():
            errs.append(f"thumbnail not found: {meta.thumbnail}")
        elif meta.thumbnail.stat().st_size > THUMBNAIL_MAX_BYTES:
            errs.append("thumbnail is over 50 MB")
    if meta.publish_at is not None:
        if meta.publish_at.tzinfo is None:
            errs.append("publish_at must include a timezone (ISO 8601, e.g. ...Z or +01:00)")
        elif meta.publish_at <= (now or datetime.now(UTC)):
            errs.append("publish_at is in the past (YouTube would publish immediately)")
    if meta.contains_synthetic_media is not True:
        errs.append("containsSyntheticMedia must be True (AI disclosure)")
    return errs


def build_video_body(meta: UploadMeta) -> dict[str, Any]:
    """The exact ``videos.insert`` body. Raises ``PrivacyViolation`` unless private."""
    if meta.privacy != DIRECT_PRIVACY:
        raise PrivacyViolation(f"refusing privacy {meta.privacy!r}: uploads are private only")
    errs = meta_problems(meta)
    if errs:
        raise MetaInvalid(errs)
    status: dict[str, Any] = {
        "privacyStatus": DIRECT_PRIVACY,
        "selfDeclaredMadeForKids": bool(meta.made_for_kids),
        "containsSyntheticMedia": True,
        "embeddable": True,
    }
    if meta.publish_at is not None:
        status["publishAt"] = meta.publish_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    snippet: dict[str, Any] = {
        "title": meta.title,
        "description": meta.description,
        "categoryId": meta.category_id,
        "defaultLanguage": meta.default_language,
        "defaultAudioLanguage": meta.default_audio_language,
    }
    if meta.tags:
        snippet["tags"] = list(meta.tags)
    return {"snippet": snippet, "status": status}


def build_insert_body(request: PublishRequest, *, category_id: str = DEFAULT_CATEGORY_ID,
                      privacy: str = DIRECT_PRIVACY) -> dict[str, Any]:
    """``PublishRequest`` adapter (pipeline long-form). Private only, like everything here."""
    platform = next((Platform(p) for p in request.platforms if Platform(p) in YOUTUBE_PLATFORMS),
                    Platform.YT_LONG)
    return build_video_body(
        UploadMeta(
            title=request.title,
            description=request.caption_by_platform.get(platform.value, ""),
            tags=list(request.tags),
            category_id=category_id,
            made_for_kids=request.made_for_kids,
            contains_synthetic_media=request.ai_disclosure,
            privacy=privacy,
        )
    )


def request_plan(video_path: Path, meta: UploadMeta) -> list[dict[str, Any]]:
    """Every API call ``upload`` would make, for ``--dry-run``. No network, no credentials."""
    calls: list[dict[str, Any]] = [
        {"method": "videos.insert", "part": "snippet,status", "body": build_video_body(meta),
         "media": {"path": str(video_path), "mimetype": "video/mp4", "resumable": True},
         "quota": QUOTA["videos.insert"]},
    ]
    if meta.thumbnail is not None:
        calls.append({"method": "thumbnails.set", "videoId": "<new video id>",
                      "media": {"path": str(meta.thumbnail),
                                "mimetype": THUMBNAIL_MIME[meta.thumbnail.suffix.lower()]},
                      "quota": QUOTA["thumbnails.set"]})
    if meta.playlist_id:
        calls.append({"method": "playlistItems.insert", "part": "snippet",
                      "body": playlist_item_body(meta.playlist_id, "<new video id>"),
                      "quota": QUOTA["playlistItems.insert"], "scope": SCOPE_YOUTUBE})
    return calls


def playlist_item_body(playlist_id: str, video_id: str) -> dict[str, Any]:
    return {"snippet": {"playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id}}}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- idempotency ledger


class YouTubeLedger:
    """``youtube_uploads`` table in the shared publish ledger (same file as Buffer's)."""

    COLS = ("content_hash", "video_sha256", "channel", "status", "video_id", "thumbnail_set",
            "playlist_id", "playlist_added", "error", "updated_at")

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or os.environ.get("STUDIO_LEDGER_PATH") or "output/ledger.sqlite")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS youtube_uploads (
                    content_hash TEXT PRIMARY KEY, video_sha256 TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending','uploaded','failed')),
                    video_id TEXT, thumbnail_set INTEGER NOT NULL DEFAULT 0,
                    playlist_id TEXT, playlist_added INTEGER NOT NULL DEFAULT 0,
                    error TEXT, updated_at TEXT NOT NULL)"""
            )

    def get(self, content_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(self.COLS)} FROM youtube_uploads WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
        return dict(zip(self.COLS, row, strict=True)) if row else None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(self.COLS)} FROM youtube_uploads ORDER BY updated_at"
            ).fetchall()
        return [dict(zip(self.COLS, r, strict=True)) for r in rows]

    def reserve(self, content_hash: str, video_sha256: str, channel: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO youtube_uploads (content_hash, video_sha256, channel, status,"
                " updated_at) VALUES (?,?,?, 'pending', ?)"
                " ON CONFLICT(content_hash) DO UPDATE SET status='pending', error=NULL,"
                " updated_at=excluded.updated_at WHERE youtube_uploads.status = 'failed'",
                (content_hash, video_sha256, channel, now),
            )

    def resolve(self, content_hash: str, status: str, *, video_id: str | None = None,
                error: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE youtube_uploads SET status=?, video_id=COALESCE(?, video_id), error=?,"
                " updated_at=? WHERE content_hash=?",
                (status, video_id, error, datetime.now(UTC).isoformat(), content_hash),
            )

    def mark(self, content_hash: str, **fields: Any) -> None:
        allowed = {"thumbnail_set", "playlist_id", "playlist_added", "error"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"mark() accepts only {sorted(allowed)}")
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE youtube_uploads SET {sets}, updated_at=? WHERE content_hash=?",
                (*fields.values(), datetime.now(UTC).isoformat(), content_hash),
            )


# ---------------------------------------------------------------- OAuth


def token_path() -> Path:
    return Path(os.environ.get("YOUTUBE_TOKEN_PATH") or DEFAULT_TOKEN_PATH).expanduser()


def scopes_for(playlists: bool) -> list[str]:
    return [SCOPE_UPLOAD, SCOPE_YOUTUBE] if playlists else [SCOPE_UPLOAD]


def _write_token(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(data)
    path.chmod(0o600)


def run_auth_flow(*, playlists: bool = False, secrets_path: str | None = None,
                  token: Path | None = None) -> Path:
    """Desktop-app loopback OAuth (PKCE). Opens a browser; caches the token with mode 0600."""
    _require_libs()
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-not-found]

    secrets = secrets_path or os.environ.get("YOUTUBE_CLIENT_SECRETS_PATH", "").strip()
    if not secrets or not Path(secrets).expanduser().is_file():
        raise FileNotFoundError(
            "set YOUTUBE_CLIENT_SECRETS_PATH to the Desktop OAuth client JSON (infra/YOUTUBE.md)"
        )
    # UNVERIFIED against the installed library: autogenerate_code_verifier enables PKCE.
    flow = InstalledAppFlow.from_client_secrets_file(
        str(Path(secrets).expanduser()), scopes=scopes_for(playlists),
        autogenerate_code_verifier=True,
    )
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
    path = token or token_path()
    _write_token(path, creds.to_json())
    return path


def load_credentials(*, need_playlists: bool = False, token: Path | None = None) -> Any:
    """Cached credentials, refreshed if expired. Never triggers a browser flow."""
    _require_libs()
    from google.auth.transport.requests import Request  # type: ignore[import-not-found]
    from google.oauth2.credentials import Credentials  # type: ignore[import-not-found]

    path = token or token_path()
    if not path.is_file():
        raise FileNotFoundError("no YouTube token cached; run `scripts/youtube_upload.py auth`")
    creds = Credentials.from_authorized_user_file(str(path))
    granted = set(creds.scopes or [])
    need = set(scopes_for(need_playlists))
    if not need <= granted:
        raise PermissionError(
            "cached token lacks scopes "
            f"{sorted(need - granted)}; re-run `auth{' --playlists' if need_playlists else ''}`"
        )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _write_token(path, creds.to_json())
        else:
            raise PermissionError("YouTube token invalid; re-run `scripts/youtube_upload.py auth`")
    return creds


def build_service(credentials: Any) -> Any:
    _require_libs()
    from googleapiclient.discovery import build  # type: ignore[import-not-found]

    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def _media(path: Path, mimetype: str, resumable: bool) -> Any:
    from googleapiclient.http import MediaFileUpload  # type: ignore[import-not-found]

    return MediaFileUpload(str(path), mimetype=mimetype, chunksize=8 * 1024 * 1024 if resumable
                           else -1, resumable=resumable)


# ---------------------------------------------------------------- upload


@dataclass
class UploadResult:
    video_id: str
    reused: bool
    thumbnail_set: bool
    playlist_added: bool
    privacy: str = DIRECT_PRIVACY

    @property
    def studio_url(self) -> str:
        return f"https://studio.youtube.com/video/{self.video_id}/edit"


def _is_definitive_rejection(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return 400 <= int(status) < 500
    except (TypeError, ValueError):
        return False


def upload(
    video_path: Path,
    meta: UploadMeta,
    *,
    request: PublishRequest,
    brand: BrandPack,
    service: Any = None,
    ledger: YouTubeLedger | None = None,
    media_factory: Callable[[Path, str, bool], Any] | None = None,
) -> UploadResult:
    """Private upload + thumbnail + optional playlist. Returns the YouTube video id.

    The gate runs first (approval bound to content_hash, AI disclosure, reviewed kids path),
    Callers must compute ``request.content_hash`` from the files on disk right before calling
    (the CLI does), so any edit after approval fails the hash check.
    """
    video_path = Path(video_path)
    if not video_path.is_file():
        raise FileNotFoundError(video_path)
    if meta.made_for_kids != brand.made_for_kids or request.made_for_kids != brand.made_for_kids:
        raise ValueError("made_for_kids must match the brand pack exactly (accurate audience)")
    assert_publishable(request, brand=brand, direct_privacy=meta.privacy)
    body = build_video_body(meta)  # raises unless private
    ledger = ledger or YouTubeLedger()
    media_factory = media_factory or _media
    key = request.content_hash
    row = ledger.get(key)
    if row and row["status"] == "pending":
        raise NeedsReconcile(
            f"an earlier upload for content {key[:12]} has an unknown outcome; check YouTube "
            "Studio and resolve the ledger row before retrying"
        )
    if service is None:
        service = build_service(load_credentials(need_playlists=bool(meta.playlist_id)))

    reused = bool(row and row["status"] == "uploaded")
    if reused:
        video_id = row["video_id"]
    else:
        ledger.reserve(key, sha256_file(video_path), request.channel)
        try:
            req = service.videos().insert(
                part="snippet,status", body=body,
                media_body=media_factory(video_path, "video/mp4", True),
            )
            resp = None
            while resp is None:
                _, resp = req.next_chunk()
        except Exception as exc:
            if _is_definitive_rejection(exc):
                ledger.resolve(key, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
            raise  # otherwise the row stays pending -> NeedsReconcile on retry
        video_id = resp["id"]
        ledger.resolve(key, "uploaded", video_id=video_id)
        reported = (resp.get("status") or {}).get("privacyStatus", DIRECT_PRIVACY)
        if reported != DIRECT_PRIVACY:
            raise PrivacyViolation(
                f"YouTube reports video {video_id} as {reported!r}, not private: set it to "
                "private in YouTube Studio now"
            )
        log.info("uploaded private video %s", video_id)

    row = ledger.get(key) or {}
    thumb_done = bool(row.get("thumbnail_set"))
    if meta.thumbnail is not None and not thumb_done:
        try:
            service.thumbnails().set(
                videoId=video_id,
                media_body=media_factory(meta.thumbnail,
                                         THUMBNAIL_MIME[meta.thumbnail.suffix.lower()], False),
            ).execute()
            ledger.mark(key, thumbnail_set=1)
            thumb_done = True
        except Exception as exc:  # noqa: BLE001 - video exists; a re-run retries the thumbnail
            ledger.mark(key, error=f"thumbnail: {type(exc).__name__}: {exc}"[:500])
            log.warning("thumbnail failed for %s: %s", video_id, exc)
    pl_done = bool(row.get("playlist_added")) and row.get("playlist_id") == meta.playlist_id
    if meta.playlist_id and not pl_done:
        try:
            service.playlistItems().insert(
                part="snippet", body=playlist_item_body(meta.playlist_id, video_id)
            ).execute()
            ledger.mark(key, playlist_id=meta.playlist_id, playlist_added=1)
            pl_done = True
        except Exception as exc:  # noqa: BLE001 - video exists; a re-run retries the playlist
            ledger.mark(key, error=f"playlist: {type(exc).__name__}: {exc}"[:500])
            log.warning("playlist insert failed for %s: %s", video_id, exc)
    return UploadResult(video_id=video_id, reused=reused, thumbnail_set=thumb_done,
                        playlist_added=bool(meta.playlist_id) and pl_done)


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
