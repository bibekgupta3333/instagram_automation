"""Free stock video/photos from the Pexels API (doc 04 §4: free, 200 req/h, 20k/month).

API (pexels.com/api/documentation, read 2026-10-16): ``Authorization: <key>`` header;
``GET /videos/search`` and ``GET /v1/search`` with ``query``, ``orientation``
(landscape|portrait|square), ``size``, ``per_page`` (<=80). Videos carry ``url`` (page),
``duration``, ``user{name,url}`` and ``video_files[{quality,file_type,width,height,link}]``;
photos carry ``url``, ``photographer``, ``photographer_url`` and ``src{original,large2x,...}``.

Terms we follow: content is free to use; credit the creator and link Pexels where possible
(the attribution goes to ``images/attribution.txt`` and ``visuals.json``, for descriptions and
audit logs); never imply that the creator or anyone shown endorses the channel; don't
replicate Pexels' core functionality; respect the rate limit (no workarounds); download
(no hotlinking). Stock can still show logos or identifiable people, so the human review gate
(doc 04 §5.4) still applies.
"""

from __future__ import annotations

import re
from typing import Any

from studio.contracts import BrandPack
from studio.stages.base import EngineUnavailable, StageError
from studio.stages.visuals.common import BeatSpec, Visual, logger

VIDEO_SEARCH = "https://api.pexels.com/videos/search"
PHOTO_SEARCH = "https://api.pexels.com/v1/search"
USER_AGENT = "studio-video-pipeline/0.1 (+https://www.pexels.com)"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MIN_CLIP_S = 4

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


def suits_stock(spec: BeatSpec, brand: BrandPack, allow_channels: tuple[str, ...] = ()) -> bool:
    """True when a real-world stock shot is a reasonable stand-in for the beat's visual."""
    if UNSTOCKABLE.search(spec.visual_prompt):
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


def _client(api_key: str, http: Any | None):
    if not api_key:
        raise EngineUnavailable("pexels needs PEXELS_API_KEY (free at pexels.com/api)")
    if http is not None:
        return http
    import httpx

    return httpx.Client(timeout=30.0, follow_redirects=True,
                        headers={"Authorization": api_key, "User-Agent": USER_AGENT})


def _get_json(http: Any, url: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = http.get(url, params=params)
    if resp.status_code == 429:
        raise StageError("pexels: rate limited (429); not retrying around the limit")
    if resp.status_code in (401, 403):
        raise EngineUnavailable(f"pexels: auth failed ({resp.status_code}); check PEXELS_API_KEY")
    if resp.status_code >= 400:
        raise StageError(f"pexels: HTTP {resp.status_code} for {url}")
    return resp.json()


def pick_video_file(video: dict[str, Any], fmt: str) -> dict[str, Any] | None:
    """Smallest mp4 rendition that still covers the frame; else the largest available."""
    want_portrait = fmt == "short"
    target = 1920 if want_portrait else 1080  # the long side for shorts, height for long-form
    files = [f for f in video.get("video_files") or []
             if (f.get("file_type") or "").endswith("mp4") and f.get("link")
             and f.get("width") and f.get("height")]
    files = [f for f in files if (f["height"] >= f["width"]) == want_portrait] or files
    if not files:
        return None
    big = [f for f in files if f["height"] >= target]
    if big:
        return min(big, key=lambda f: f["width"] * f["height"])
    return max(files, key=lambda f: f["width"] * f["height"])


def pick_video(videos: list[dict[str, Any]], fmt: str, used: set[str]) -> tuple | None:
    want_portrait = fmt == "short"
    best: tuple[tuple, dict, dict] | None = None
    for v in videos:
        if f"video:{v.get('id')}" in used:
            continue
        f = pick_video_file(v, fmt)
        if f is None:
            continue
        portrait = (f["height"] >= f["width"])
        score = (
            portrait == want_portrait,
            (v.get("duration") or 0) >= MIN_CLIP_S,
            min(f["height"], f["width"]) >= (1080 if want_portrait else 720),
        )
        if best is None or score > best[0]:
            best = (score, v, f)
    return (best[1], best[2]) if best else None


def _download(http: Any, url: str, out, max_bytes: int = MAX_DOWNLOAD_BYTES) -> None:
    tmp = out.with_suffix(out.suffix + ".part")
    size = 0
    with http.stream("GET", url) as resp:
        if resp.status_code >= 400:
            raise StageError(f"pexels: download HTTP {resp.status_code}")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    fh.close()
                    tmp.unlink(missing_ok=True)
                    raise StageError("pexels: download exceeded size cap")
                fh.write(chunk)
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise StageError("pexels: empty download")
    tmp.replace(out)


def fetch(
    spec: BeatSpec,
    *,
    api_key: str,
    used: set[str] | None = None,
    http: Any | None = None,
    prefer_video: bool = True,
) -> Visual:
    """Search by the beat's keywords, prefer a clip in the frame's orientation, download it."""
    used = used if used is not None else set()
    query = keywords(spec.visual_prompt)
    if not query:
        raise StageError("pexels: no keywords in visual_prompt")
    client = _client(api_key, http)
    owns = http is None
    try:
        if prefer_video:
            data = _get_json(client, VIDEO_SEARCH, {"query": query, "orientation": spec.orientation,
                                                    "per_page": 15})
            picked = pick_video(data.get("videos") or [], spec.fmt, used)
            if picked:
                video, vfile = picked
                out = spec.out_stem.with_suffix(".mp4")
                _download(client, vfile["link"], out)
                user = video.get("user") or {}
                used.add(f"video:{video.get('id')}")
                return Visual(path=out, kind="video", engine="pexels", model="pexels-video",
                              source={
                                  "provider": "Pexels", "license": "Pexels License",
                                  "id": video.get("id"), "query": query,
                                  "source_url": video.get("url"), "file_url": vfile["link"],
                                  "creator": user.get("name"), "creator_url": user.get("url"),
                                  "width": vfile["width"], "height": vfile["height"],
                                  "duration": video.get("duration"),
                                  "credit": _credit("Video", user.get("name"), video.get("url")),
                              })
            logger.info("pexels: no usable video for %r, trying photos", query)
        data = _get_json(client, PHOTO_SEARCH, {"query": query, "orientation": spec.orientation,
                                                "per_page": 15})
        for photo in data.get("photos") or []:
            if f"photo:{photo.get('id')}" in used:
                continue
            src = photo.get("src") or {}
            link = src.get("large2x") or src.get("original")
            if not link:
                continue
            out = spec.out_stem.with_suffix(".jpg")
            _download(client, link, out)
            used.add(f"photo:{photo.get('id')}")
            return Visual(path=out, kind="image", engine="pexels", model="pexels-photo", source={
                "provider": "Pexels", "license": "Pexels License", "id": photo.get("id"),
                "query": query, "source_url": photo.get("url"), "file_url": link,
                "creator": photo.get("photographer"),
                "creator_url": photo.get("photographer_url"),
                "credit": _credit("Photo", photo.get("photographer"), photo.get("url")),
            })
        raise StageError(f"pexels: nothing found for {query!r}")
    finally:
        if owns:
            client.close()


def _credit(kind: str, name: str | None, url: str | None) -> str:
    # Plain credit only: no wording that suggests the creator endorses the channel.
    return f"{kind} by {name or 'unknown'} on Pexels: {url or 'https://www.pexels.com'}"
