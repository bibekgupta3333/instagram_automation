"""Publish gate: the last hard stop before any post leaves the machine (docs 05 §3/§6, 16 §6).

``assert_publishable(request)`` raises ``PublishBlocked`` (a PermissionError) listing every
problem, unless:
- a human approval is bound to this exact content (approved_hash == content_hash),
- media/thumbnail URLs are public https and NOT pre-signed/expiring (Buffer fetches at publish
  time; docs 05 gotcha 6),
- ai_disclosure is True,
- made_for_kids is False, EXCEPT on the reviewed kids path below,
- per-platform caption/title/hashtag limits from docs 05 §3 hold.

Reviewed kids path (user decision 2026-10-23, doc 26). ``made_for_kids=True`` is allowed ONLY when
all of these hold; anything else is blocked:
- the caller passes the channel's ``BrandPack`` and it has ``made_for_kids: true`` (and its slug
  is the request's channel),
- every platform is YouTube (Shorts/long-form): no Instagram or Facebook for child audiences,
- it is a direct YouTube Data API upload (``direct_privacy`` given) and the privacy is "private":
  the human reviews in YouTube Studio and clicks Publish.
A kids brand can never be declared ``made_for_kids=False`` (that would mislabel child-directed
content), and adult brands keep ``made_for_kids=False`` exactly as before.

``direct_privacy`` marks a direct YouTube API upload of local bytes (``studio/publish/youtube.py``).
Only "private" is accepted, the platforms must all be YouTube, and the public-URL checks are
skipped because nothing is fetched from a URL (they protect Buffer, which fetches at publish time);
the uploader verifies the local file's sha256 against the approval instead.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from studio.contracts import BrandPack, Platform, PublishRequest


@dataclass(frozen=True)
class PlatformLimits:
    caption: int | None
    title: int | None = None
    max_hashtags: int | None = None
    first_comment: int | None = None


# docs 05 §3 (all [V] unless noted).
LIMITS: dict[str, PlatformLimits] = {
    Platform.YT_SHORTS: PlatformLimits(caption=5000, title=100),
    Platform.YT_LONG: PlatformLimits(caption=5000, title=100),  # YouTube Data API limits
    Platform.IG_REELS: PlatformLimits(caption=2200, max_hashtags=5, first_comment=2196),
    # UNVERIFIED: docs 05 gives no FB caption limit ("Text field"). 63,206 is Facebook's general
    # post-text limit; verify against Buffer's composer before relying on it.
    Platform.FB_REELS: PlatformLimits(caption=63206),
}

# Query params that mark a signed/expiring URL (S3 SigV2/V4, CloudFront, GCS, Azure SAS).
PRESIGN_PARAMS = frozenset(
    {
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-expires",
        "x-amz-security-token",
        "signature",
        "expires",
        "key-pair-id",
        "x-goog-signature",
        "x-goog-credential",
        "x-goog-expires",
        "googleaccessid",
        "sig",
        "se",
    }
)
# Hosts docs 05 says not to use (not permanent / not direct media).
BLOCKED_HOSTS = ("drive.google.com", "docs.google.com", "dropbox.com", "dl.dropboxusercontent.com")

YOUTUBE_PLATFORMS = frozenset({Platform.YT_SHORTS, Platform.YT_LONG})
DIRECT_PRIVACY = "private"  # the only privacy a direct upload may use (user decision 2026-10-23)

HASHTAG = re.compile(r"(?<!\w)#\w+")


class PublishBlocked(PermissionError):
    def __init__(self, errors: list[str]):
        super().__init__("publish blocked: " + "; ".join(errors))
        self.errors = errors


def url_problems(url: str | None, label: str) -> list[str]:
    if not url:
        return [f"{label} is empty"]
    parts = urlsplit(url)
    errs: list[str] = []
    if parts.scheme != "https":
        errs.append(f"{label} must be https (got {parts.scheme or 'no scheme'!r})")
    host = (parts.hostname or "").lower()
    if not host:
        errs.append(f"{label} has no host")
    elif host == "localhost" or host.endswith((".local", ".internal")):
        errs.append(f"{label} host {host!r} is not public")
    else:
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                errs.append(f"{label} host {host!r} is not a public address")
        except ValueError:
            pass
    if any(host == h or host.endswith("." + h) for h in BLOCKED_HOSTS):
        errs.append(f"{label} uses {host}; use a permanent public CDN URL (docs 05)")
    signed = sorted(
        {
            k
            for k, _ in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() in PRESIGN_PARAMS
        }
    )
    if signed:
        errs.append(
            f"{label} looks pre-signed/expiring (query params {signed}); "
            "Buffer needs a permanent public URL"
        )
    return errs


def caption_problems(request: PublishRequest) -> list[str]:
    errs: list[str] = []
    for platform in request.platforms:
        p = Platform(platform)
        lim = LIMITS[p]
        cap = request.caption_by_platform.get(p.value)
        if cap is None:
            errs.append(f"no caption for {p.value}")
            continue
        if lim.caption is not None and len(cap) > lim.caption:
            errs.append(f"{p.value} caption is {len(cap)} chars (max {lim.caption})")
        if lim.max_hashtags is not None:
            n = len(HASHTAG.findall(cap))
            if n > lim.max_hashtags:
                errs.append(f"{p.value} caption has {n} hashtags (max {lim.max_hashtags})")
        if lim.title is not None:
            if not request.title.strip():
                errs.append(f"{p.value} needs a title")
            elif len(request.title) > lim.title:
                errs.append(f"{p.value} title is {len(request.title)} chars (max {lim.title})")
    return errs


def kids_problems(
    request: PublishRequest, brand: BrandPack | None, direct_privacy: str | None
) -> list[str]:
    """made_for_kids rules: blocked unless the reviewed kids path holds (module docstring)."""
    errs: list[str] = []
    if brand is not None and brand.slug != request.channel:
        errs.append(f"brand pack {brand.slug!r} does not match request channel {request.channel!r}")
    if request.made_for_kids is not True:
        if brand is not None and brand.made_for_kids:
            errs.append(
                f"brand {brand.slug!r} is made for kids: the request must declare "
                "made_for_kids=True (accurate audience setting, doc 26)"
            )
        return errs
    if brand is None or not brand.made_for_kids:
        errs.append(
            "made_for_kids must be False for this portfolio (only a brand pack with "
            "made_for_kids: true may use the reviewed kids path)"
        )
    non_yt = [Platform(p).value for p in request.platforms if Platform(p) not in YOUTUBE_PLATFORMS]
    if non_yt:
        errs.append(f"made_for_kids content may only go to YouTube, not {non_yt} (doc 26)")
    if direct_privacy != DIRECT_PRIVACY:
        errs.append(
            "made_for_kids content may only be a private direct YouTube upload "
            f"(got privacy {direct_privacy!r}); a human publishes it in YouTube Studio"
        )
    return errs


def publish_problems(
    request: PublishRequest,
    *,
    brand: BrandPack | None = None,
    direct_privacy: str | None = None,
) -> list[str]:
    errs: list[str] = []
    if not request.content_hash or not request.approved_hash:
        errs.append("missing content_hash or approved_hash: no recorded human approval")
    elif request.approved_hash != request.content_hash:
        errs.append("approved_hash != content_hash: content changed after approval")
    if not request.platforms:
        errs.append("no platforms")
    errs += kids_problems(request, brand, direct_privacy)
    if request.ai_disclosure is not True:
        errs.append("ai_disclosure must be set (isAiGenerated / containsSyntheticMedia)")
    if direct_privacy is None:
        errs += url_problems(request.media_url, "media_url")
        if request.thumbnail_url:
            errs += url_problems(request.thumbnail_url, "thumbnail_url")
    else:
        if direct_privacy != DIRECT_PRIVACY:
            errs.append(f"direct YouTube uploads must be private (got {direct_privacy!r})")
        non_yt = [
            Platform(p).value for p in request.platforms if Platform(p) not in YOUTUBE_PLATFORMS
        ]
        if non_yt:
            errs.append(f"a direct YouTube upload cannot target {non_yt}")
    errs += caption_problems(request)
    return list(dict.fromkeys(errs))


def assert_publishable(
    request: PublishRequest,
    *,
    brand: BrandPack | None = None,
    direct_privacy: str | None = None,
) -> None:
    errs = publish_problems(request, brand=brand, direct_privacy=direct_privacy)
    if errs:
        raise PublishBlocked(errs)


def check_first_comment(platform: str, text: str | None) -> None:
    lim = LIMITS[Platform(platform)].first_comment
    if text and lim is not None and len(text) > lim:
        raise PublishBlocked([f"{platform} first comment is {len(text)} chars (max {lim})"])
