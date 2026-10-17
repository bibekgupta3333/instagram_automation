"""Free stock video/photos from the Pixabay API (free key, 100 requests / 60 s per key).

API (pixabay.com/api/docs, read 2026-10-16):
- Photos: ``GET https://pixabay.com/api/`` with ``key``, ``q`` (<=100 chars), ``image_type``
  (all|photo|illustration|vector), ``orientation`` (all|horizontal|vertical), ``min_width``,
  ``min_height``, ``editors_choice``, ``safesearch``, ``order`` (popular|latest), ``page``,
  ``per_page`` (3-200). Hits carry ``id``, ``pageURL``, ``tags``, ``user``, ``user_id``,
  ``webformatURL`` (<=640 px), ``largeImageURL`` (<=1280 px), ``imageWidth``/``imageHeight``
  (the original), and ``fullHDURL`` (<=1920 px, full-API-access accounts only).
- Videos: ``GET https://pixabay.com/api/videos/`` with the same params minus
  ``image_type``/``orientation`` (there is NO orientation filter for videos; we pick by the
  rendition's width/height) plus ``video_type`` (all|film|animation). Hits carry ``duration``
  and ``videos.{large,medium,small,tiny}.{url,width,height,size,thumbnail}``; ``large`` (often
  3840x2160) may be empty with size 0.
- Rate limit: 100 requests per 60 s per key; ``X-RateLimit-Limit``/``-Remaining``/``-Reset``
  headers; HTTP 429 when exceeded. We pause when Remaining hits 0 and back off on 429.

API rules we follow: responses are cached for 24 h (``output/.cache/pixabay/<sha>.json``);
no permanent hotlinking (every file is downloaded into the job's images dir); no systematic
mass downloads (one search per beat, a few results); show the source - we log the creator
and page URL to ``images/attribution.txt`` and ``visuals.json``.

Pixabay Content License (pixabay.com/service/license-summary, read 2026-10-16): free to use,
modify, and use commercially; attribution is NOT required (but appreciated). NOT allowed:
selling/distributing content on a standalone (unaltered) basis; commercial use of content
showing recognisable trademarks, logos or brands; immoral/illegal use, especially of content
featuring recognisable people; misleading or deceptive use (e.g. implying a person or brand
shown endorses the channel or the claim); using content as a trademark/business name. Stock
can still contain logos or identifiable people, so the human review gate (doc 04 §5.4) applies.

The API key travels as a query parameter, so every URL we log goes through ``redact()`` and a
filter on the ``httpx`` logger strips it from httpx's own request log lines.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from studio.contracts import BrandPack
from studio.stages.base import EngineUnavailable, StageError
from studio.stages.visuals.common import BeatSpec, Visual, logger
from studio.stages.visuals.prompts import shot_class

IMAGE_SEARCH = "https://pixabay.com/api/"
VIDEO_SEARCH = "https://pixabay.com/api/videos/"
USER_AGENT = "studio-video-pipeline/0.1 (+https://pixabay.com/api/docs/)"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MIN_CLIP_S = 4
MIN_SHORT_SIDE = 1080
CACHE_TTL_S = 24 * 3600
PER_PAGE = 20
MAX_429_RETRIES = 3
MAX_DOWNLOAD_REDIRECTS = 3
DOWNLOAD_HOSTS = frozenset({"pixabay.com", "www.pixabay.com", "cdn.pixabay.com"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RENDITIONS = ("large", "medium", "small", "tiny")

# Visuals that stock footage can't plausibly match (renders, diagrams, UI, illustration).
UNSTOCKABLE = re.compile(
    r"\b(cutaway|cross[- ]section|diagram|isometric|blueprint|render(ed|ing)?|3d|cgi|vector|"
    r"infographic|chart|graph|dashboard|illustrat\w*|cartoon|anime|comic|ui|screenshot|"
    r"interface|icon|map|schematic|exploded view|pixel art|mascot|character|logo|text overlay)\b",
    re.IGNORECASE,
)
STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and", "or", "from", "into",
    "over", "under", "by", "as", "is", "are", "be", "this", "that", "its", "it", "their", "his",
    "her", "shot", "close", "closeup", "close-up", "up", "wide", "angle", "view", "showing",
    "shows", "scene", "image", "photo", "picture", "cinematic", "dramatic", "detailed",
    "realistic", "style", "background", "foreground", "very", "soft", "hard", "light",
    "lighting", "lit", "top", "down", "side", "front", "back", "left", "right", "center",
    "centre", "camera", "lens", "frame", "vertical", "horizontal", "composition", "b-roll",
    "broll", "footage", "clip",
})

_KEY_RE = re.compile(r"(?i)\b(key=)[^&\s'\"]+")

# Search relevance is not guaranteed by Pixabay. Ambiguous terms such as "fuel" also
# retrieve firewood; a query match alone is never evidence that a hit depicts a car.
DOMAIN_ANCHORS = {"car-cost-truth": "car"}
_CAR_QUERY_ANCHOR = re.compile(r"\b(?:cars?|vehicles?)\b", re.IGNORECASE)
_CAR_TAGS = re.compile(
    r"\b(?:cars?|vehicles?|driving|roads?|engines?|automobiles?|automotive|highways?|"
    r"traffic|trucks?|sedans?|motorways?)\b", re.IGNORECASE,
)


def anchor_query(query: str, channel: str = "") -> str:
    """Keep the channel subject in each search, including shortened and visual fallbacks."""
    query = " ".join(re.findall(r"[a-zA-Z0-9][a-zA-Z0-9'-]*", query.lower()))
    if not query:
        return ""
    anchor = DOMAIN_ANCHORS.get(channel)
    if anchor and not _CAR_QUERY_ANCHOR.search(query[:100]):
        query = f"{anchor} {query}"
    return query[:100].rstrip()


def relevant_hit(hit: dict[str, Any], channel: str) -> bool:
    """Require domain evidence in the asset's tags, never its query or download URL.

    Missing tags fail closed for anchored channels. This is applied after search/cache
    lookup and before either quality ranking or download, for both videos and photos.
    """
    if channel not in DOMAIN_ANCHORS:
        return True
    tags = hit.get("tags")
    return isinstance(tags, str) and bool(_CAR_TAGS.search(tags))


def redact(text: str) -> str:
    """Replace any ``key=...`` query value with ``key=***``."""
    return _KEY_RE.sub(r"\1***", str(text))


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        if "key=" in msg.lower():
            record.msg, record.args = redact(msg), None
        return True


_FILTER = _RedactFilter()
for _name in ("httpx", "httpcore", logger.name):
    if not any(isinstance(f, _RedactFilter) for f in logging.getLogger(_name).filters):
        logging.getLogger(_name).addFilter(_FILTER)


def stock_subject(spec: BeatSpec) -> bool:
    """A stock asset cannot stand in for a requested technical illustration."""
    return not (UNSTOCKABLE.search(spec.visual_prompt) or UNSTOCKABLE.search(spec.shot)
                or shot_class(spec.shot) in ("diagram", "cutaway"))


def suits_stock(spec: BeatSpec, brand: BrandPack, allow_channels: tuple[str, ...] = ()) -> bool:
    """True when a real-world stock shot is a reasonable stand-in for the beat's visual."""
    if not stock_subject(spec):
        return False
    if "all" in allow_channels or brand.slug in allow_channels:
        return True
    return not UNSTOCKABLE.search(brand.style_prompt or "")


def keywords(visual_prompt: str, limit: int = 4) -> str:
    words = re.findall(r"[a-zA-Z][a-zA-Z'-]+", visual_prompt.lower())
    picked: list[str] = []
    for w in words:
        w = w.strip("'-")
        if len(w) > 2 and w not in STOPWORDS and w not in picked:
            picked.append(w)
        if len(picked) == limit:
            break
    return " ".join(picked)


def queries(visual_prompt: str, stock_query: str = "", channel: str = "") -> list[str]:
    """Pixabay ANDs the terms. The script's ``stock_query`` comes first (it names the real
    subject, unlike a styled image prompt), then its first 2 words; keyword extraction from the
    visual prompt is the fallback (4 keywords, then 2)."""
    out: list[str] = []
    sq = " ".join(re.findall(r"[a-zA-Z0-9][a-zA-Z0-9'-]*", stock_query.lower()))[:100]
    if sq:
        out += [sq, " ".join(sq.split()[:2])]
    out += [keywords(visual_prompt, 4), keywords(visual_prompt, 2)]
    return list(dict.fromkeys(anchored for q in out if (anchored := anchor_query(q, channel))))


# ---------------------------------------------------------------- rate limit + cache


class RateLimiter:
    """Tracks X-RateLimit-* headers; sleeps before a request when the window is used up."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep):
        self.sleep = sleep
        self.remaining: int | None = None
        self.reset_at = 0.0
        self.last_headers: dict[str, str] = {}

    def before(self) -> None:
        if self.remaining is not None and self.remaining <= 0:
            wait = self.reset_at - time.monotonic()
            if wait > 0:
                logger.info("pixabay: rate window used up; sleeping %.1fs", wait)
                self.sleep(min(wait, 61.0))
            self.remaining = None

    def after(self, headers: Any) -> None:
        self.last_headers = {k: headers[k] for k in
                             ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset")
                             if k in headers}
        try:
            self.remaining = int(headers.get("X-RateLimit-Remaining"))
            self.reset_at = time.monotonic() + float(headers.get("X-RateLimit-Reset") or 60)
        except (TypeError, ValueError):
            pass


LIMITER = RateLimiter()


def _cache_file(cache_dir: Path, url: str, params: dict[str, Any]) -> Path:
    public = {k: v for k, v in sorted(params.items()) if k != "key"}  # never hash/store the key
    digest = hashlib.sha256(json.dumps([url, public], sort_keys=True).encode()).hexdigest()
    return cache_dir / f"{digest}.json"


def _read_cache(path: Path, now: float) -> dict[str, Any] | None:
    try:
        blob = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if now - float(blob.get("fetched_at", 0)) > CACHE_TTL_S:
        return None
    return blob.get("data")


def search(http: Any, url: str, params: dict[str, Any], *, api_key: str,
           cache_dir: Path | None = None, limiter: RateLimiter | None = None,
           sleep: Callable[[float], None] = time.sleep,
           now: Callable[[], float] = time.time) -> dict[str, Any]:
    """GET a search endpoint with a 24 h on-disk cache, rate limiting and 429 backoff."""
    limiter = limiter or LIMITER
    cache = _cache_file(cache_dir, url, params) if cache_dir is not None else None
    if cache is not None:
        hit = _read_cache(cache, now())
        if hit is not None:
            logger.info("pixabay: cache hit for q=%r", params.get("q"))
            return hit
    full = {"key": api_key, **params}
    for attempt in range(MAX_429_RETRIES + 1):
        limiter.before()
        try:
            resp = http.get(url, params=full, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - httpx errors embed the URL (with the key)
            raise StageError(f"pixabay: request failed: {redact(exc)}") from None
        limiter.after(resp.headers)
        if resp.status_code == 429:
            if attempt == MAX_429_RETRIES:
                raise StageError("pixabay: rate limited (429) after backoff")
            try:
                wait = float(resp.headers.get("X-RateLimit-Reset") or 0)
            except ValueError:
                wait = 0.0
            wait = max(wait, 2.0 ** attempt)
            logger.warning("pixabay: 429 on %s; backing off %.1fs", redact(str(resp.url)), wait)
            sleep(min(wait, 61.0))
            continue
        if resp.status_code in (400, 401, 403) and "key" in (resp.text or "").lower():
            raise EngineUnavailable(f"pixabay: auth failed ({resp.status_code}); "
                                    "check PIXABAY_API_KEY")
        if resp.status_code >= 300:
            raise StageError(f"pixabay: HTTP {resp.status_code} for {redact(str(resp.url))}")
        data = resp.json()
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".part")
            tmp.write_text(json.dumps({"fetched_at": now(), "url": url,
                                       "params": {k: v for k, v in params.items()},
                                       "data": data}))
            tmp.replace(cache)
        return data
    raise StageError("pixabay: unreachable")  # pragma: no cover


# ---------------------------------------------------------------- selection


def _renditions(hit: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for name in RENDITIONS:
        r = (hit.get("videos") or {}).get(name) or {}
        if r.get("url") and r.get("width") and r.get("height") and (r.get("size") or 1) > 0 \
                and (r.get("size") or 0) <= MAX_DOWNLOAD_BYTES:
            out.append({**r, "rendition": name})
    return out


def _frame(fmt: str) -> tuple[int, int]:
    return (1080, 1920) if fmt == "short" else (1920, 1080)


def cover_scale(width: int, height: int, fmt: str) -> float:
    """How much render must enlarge a clip to cover the frame (centre crop); <= 1 is sharp.
    A 1920x1080 clip in a 9:16 Short is cropped to 608x1080 and enlarged 1.78x (soft); the 4K
    rendition only 0.89x."""
    fw, fh = _frame(fmt)
    return max(fw / width, fh / height)


def pick_rendition(hit: dict[str, Any], fmt: str | None = None) -> dict[str, Any] | None:
    """Smallest rendition that covers the frame without enlarging (with `fmt`), else the
    smallest whose short side is >= 1080, else the largest available."""
    files = _renditions(hit)
    if not files:
        return None
    if fmt is not None:
        sharp = [f for f in files if cover_scale(f["width"], f["height"], fmt) <= 1.0]
        if sharp:
            return min(sharp, key=lambda f: f["width"] * f["height"])
    big = [f for f in files if min(f["width"], f["height"]) >= MIN_SHORT_SIDE]
    if big:
        return min(big, key=lambda f: f["width"] * f["height"])
    return max(files, key=lambda f: f["width"] * f["height"])


def pick_video(hits: list[dict[str, Any]], fmt: str, used: set[str]) -> tuple | None:
    """Prefer: sharp after the cover crop (enlarged <= 1.15x), frame orientation (vertical
    for shorts), >= 4 s, short side >= 1080, then Pixabay's relevance order."""
    want_vertical = fmt == "short"
    best: tuple[tuple, dict, dict] | None = None
    for i, hit in enumerate(hits):
        if f"video:{hit.get('id')}" in used:
            continue
        f = pick_rendition(hit, fmt)
        if f is None:
            continue
        score = (
            cover_scale(f["width"], f["height"], fmt) <= 1.15,
            (f["height"] > f["width"]) == want_vertical,
            (hit.get("duration") or 0) >= MIN_CLIP_S,
            min(f["width"], f["height"]) >= MIN_SHORT_SIDE,
            -i,  # keep Pixabay's relevance order as the tie-break
        )
        if best is None or score > best[0]:
            best = (score, hit, f)
    return (best[1], best[2]) if best else None


def pick_photo(hits: list[dict[str, Any]], used: set[str]) -> tuple | None:
    for hit in hits:
        if f"photo:{hit.get('id')}" in used:
            continue
        link = hit.get("fullHDURL") or hit.get("largeImageURL")
        if link:
            cap = 1920 if hit.get("fullHDURL") else 1280
            w, h = hit.get("imageWidth") or 0, hit.get("imageHeight") or 0
            scale = min(1.0, cap / max(w, h)) if w and h else 1.0
            return hit, link, (round(w * scale), round(h * scale))
    return None


# ---------------------------------------------------------------- fetch


def _client(api_key: str, http: Any | None):
    if not api_key:
        raise EngineUnavailable("pixabay needs PIXABAY_API_KEY (free at pixabay.com/api/docs)")
    if http is not None:
        return http
    import httpx

    return httpx.Client(timeout=60.0, follow_redirects=False, headers={"User-Agent": USER_AGENT})


def _validate_download_url(url: str) -> None:
    """Only exact Pixabay HTTPS authorities; never trust an API/cache URL or redirect."""
    try:
        if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127
                                           or c == "\\" for c in url):
            raise ValueError
        parsed = urlsplit(url)
        # Compare the entire authority, rejecting credentials, suffix tricks, encoded
        # hosts, IP literals and every port spelling except an explicit :443.
        authority = parsed.netloc.lower()
        allowed = DOWNLOAD_HOSTS | {f"{host}:443" for host in DOWNLOAD_HOSTS}
        if parsed.scheme != "https" or authority not in allowed:
            raise ValueError
    except ValueError:
        # Don't echo URLs: rejected credentials or query parameters may be secrets.
        raise StageError("pixabay: unsafe download URL") from None


def _download(http: Any, url: str, out: Path, max_bytes: int = MAX_DOWNLOAD_BYTES) -> int:
    import httpx

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    size = 0
    try:
        for redirects in range(MAX_DOWNLOAD_REDIRECTS + 1):
            _validate_download_url(url)
            # Override even injected clients configured to follow redirects.
            with http.stream("GET", url, follow_redirects=False) as resp:
                if resp.status_code in REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if not location or redirects == MAX_DOWNLOAD_REDIRECTS:
                        raise StageError("pixabay: missing redirect location or too many redirects")
                    # Validate the raw header before urljoin can discard control characters.
                    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\"
                           for c in location):
                        raise StageError("pixabay: unsafe download URL")
                    url = urljoin(url, location)
                    continue
                if not 200 <= resp.status_code < 300:
                    raise StageError(f"pixabay: download HTTP {resp.status_code}")
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise StageError("pixabay: download exceeded size cap")
                        fh.write(chunk)
                break
    except StageError:
        tmp.unlink(missing_ok=True)
        raise
    except (httpx.InvalidURL, httpx.RemoteProtocolError, ValueError):
        tmp.unlink(missing_ok=True)
        raise StageError("pixabay: unsafe download URL") from None
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise StageError(f"pixabay: download failed: {redact(exc)}") from None
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise StageError("pixabay: empty download")
    tmp.replace(out)
    return size


def credit(kind: str, user: str | None, page_url: str | None, asset_id: Any) -> str:
    # Plain credit only: no wording that suggests the creator endorses the channel.
    return (f"{kind} by {user or 'unknown'} on Pixabay (id {asset_id}): "
            f"{page_url or 'https://pixabay.com'}")


def _source(kind: str, hit: dict[str, Any], query: str, file_url: str, w: int, h: int,
            **extra: Any) -> dict[str, Any]:
    return {
        "provider": "Pixabay", "license": "Pixabay Content License",
        "license_url": "https://pixabay.com/service/license-summary/",
        "id": hit.get("id"), "query": query, "source_url": hit.get("pageURL"),
        "file_url": file_url, "creator": hit.get("user"), "creator_id": hit.get("user_id"),
        "tags": hit.get("tags"), "width": w, "height": h, **extra,
        "credit": credit(kind, hit.get("user"), hit.get("pageURL"), hit.get("id")),
    }


def fetch(
    spec: BeatSpec,
    *,
    api_key: str,
    used: set[str] | None = None,
    http: Any | None = None,
    prefer_video: bool = True,
    cache_dir: Path | None = None,
    limiter: RateLimiter | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Visual:
    """Search by the beat's keywords, prefer a clip in the frame's orientation, else a photo;
    download it next to the beat. ``used`` holds ``video:<id>``/``photo:<id>`` already in this
    video and is updated."""
    if not stock_subject(spec):
        raise EngineUnavailable("pixabay: diagram/cutaway or illustrated beat needs a local still")
    used = used if used is not None else set()
    qs = queries(spec.visual_prompt, spec.stock_query, spec.channel)
    if not qs:
        raise StageError("pixabay: no keywords in stock_query / visual_prompt")
    client = _client(api_key, http)
    owns = http is None
    common = {"safesearch": "true", "per_page": PER_PAGE}
    kw = {"api_key": api_key, "cache_dir": cache_dir, "limiter": limiter, "sleep": sleep}
    vertical = spec.fmt == "short"
    try:
        if prefer_video:
            for q in qs:
                data = search(client, VIDEO_SEARCH, {"q": q, "video_type": "film", **common}, **kw)
                hits = [hit for hit in data.get("hits") or [] if relevant_hit(hit, spec.channel)]
                picked = pick_video(hits, spec.fmt, used)
                if picked:
                    hit, f = picked
                    out = spec.out_stem.with_suffix(".mp4")
                    _download(client, f["url"], out)
                    used.add(f"video:{hit.get('id')}")
                    return Visual(path=out, kind="video", engine="pixabay", model="pixabay-video",
                                  source=_source("Video", hit, q, f["url"], f["width"],
                                                 f["height"], duration=hit.get("duration"),
                                                 rendition=f["rendition"]))
            logger.info("pixabay: no usable video for %r, trying photos", qs[0])
        w, h = (1080, 1920) if vertical else (1920, 1080)
        for q in qs:
            data = search(client, IMAGE_SEARCH, {
                "q": q, "image_type": "photo",
                "orientation": "vertical" if vertical else "horizontal",
                "min_width": w, "min_height": h, **common}, **kw)
            hits = [hit for hit in data.get("hits") or [] if relevant_hit(hit, spec.channel)]
            picked = pick_photo(hits, used)
            if picked:
                hit, link, (pw, ph) = picked
                out = spec.out_stem.with_suffix(".jpg")
                _download(client, link, out)
                used.add(f"photo:{hit.get('id')}")
                return Visual(path=out, kind="image", engine="pixabay", model="pixabay-photo",
                              source=_source("Photo", hit, q, link, pw, ph))
        raise StageError(f"pixabay: nothing found for {qs[0]!r}")
    finally:
        if owns:
            client.close()
