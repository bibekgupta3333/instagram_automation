"""Direct YouTube Data API long-form upload (STUB). Buffer cannot post long-form YouTube
(docs 05 §1/§3), so long-form goes through ``videos.insert``.

Needs ``google-api-python-client`` + ``google-auth-oauthlib`` (not in pyproject; not installed).
Without them ``upload`` raises NotImplementedError.

Audit note (docs 05 §9): API projects that have not passed Google's YouTube API compliance
audit can only upload **private** videos. Until ``YOUTUBE_PROJECT_AUDITED=1`` this module forces
``privacyStatus=private`` (a human then flips visibility in Studio). Start the audit on day 1.
Upload quota (1,600 of 10,000 units/day per insert vs a dedicated bucket) is disputed [3P].

OAuth scope: ``youtube.upload`` held only by this Publisher; agents get read-only (docs 16 §6).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from studio.contracts import Platform, PublishRequest
from studio.publish.gate import assert_publishable

# YouTube category ids, e.g. 27 = Education, 28 = Science & Technology.
DEFAULT_CATEGORY_ID = "27"


def google_libs_available() -> bool:
    return all(
        importlib.util.find_spec(m) is not None for m in ("googleapiclient", "google_auth_oauthlib")
    )


def build_insert_body(
    request: PublishRequest,
    *,
    category_id: str = DEFAULT_CATEGORY_ID,
    privacy: str = "private",
    audited: bool | None = None,
) -> dict[str, Any]:
    if audited is None:
        audited = os.environ.get("YOUTUBE_PROJECT_AUDITED") == "1"
    if not audited:
        privacy = "private"  # unaudited projects: private-only uploads
    status: dict[str, Any] = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": bool(request.ai_disclosure),  # altered/synthetic disclosure
        "embeddable": True,
    }
    if request.scheduled_at is not None and audited:
        status["privacyStatus"] = "private"  # publishAt requires private
        status["publishAt"] = request.scheduled_at.isoformat()
    return {
        "snippet": {
            "title": request.title[:100],
            "description": request.caption_by_platform.get(Platform.YT_LONG.value, "")[:5000],
            "tags": request.tags,
            "categoryId": category_id,
        },
        "status": status,
    }


def upload(request: PublishRequest, video_path: Path, *, credentials: Any = None) -> str:
    """Upload a long-form video; returns the YouTube video id."""
    assert_publishable(request)
    if Platform.YT_LONG not in request.platforms:
        raise ValueError("request does not target youtube_long")
    if not google_libs_available():
        raise NotImplementedError(
            "Direct YouTube upload needs google-api-python-client and google-auth-oauthlib "
            "(not installed). Note: unaudited API projects can upload private videos only "
            "(docs 05 §9)."
        )
    if credentials is None:
        raise NotImplementedError("OAuth (youtube.upload scope) flow not wired yet")
    from googleapiclient.discovery import build  # type: ignore[import-not-found]
    from googleapiclient.http import MediaFileUpload  # type: ignore[import-not-found]

    yt = build("youtube", "v3", credentials=credentials)
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=-1, resumable=True)
    req = yt.videos().insert(
        part="snippet,status", body=build_insert_body(request), media_body=media
    )
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]
