"""Dry-run publish for one approved job (child process of the dashboard action).

DRY_RUN is forced to 1 and the Buffer client is constructed with ``dry_run=True`` and an
in-memory ledger, so nothing is sent, nothing is reserved and no credentials are read.
The hard publish gate still runs. The result is printed to the action log only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    os.environ["DRY_RUN"] = "1"
    os.environ.pop("BUFFER_API_KEY", None)
    if len(argv) < 2:
        print("usage: publish_dry JOB_ID PLATFORM...", file=sys.stderr)
        return 2
    job_id, platforms = argv[0], argv[1:]
    from studio.config import get_settings, load_brand
    from studio.contracts import Platform, PublishRequest
    from studio.pipeline.runner import load_job, verify_approval
    from studio.publish.buffer import BufferPublisher, PostLedger
    from studio.publish.gate import PublishBlocked

    settings = get_settings()
    job = load_job(job_id, settings)
    if not verify_approval(job):
        print(f"{job_id}: approval is not valid for the current content; nothing to dry-run")
        return 1
    base = os.environ.get("CDN_PUBLIC_BASE_URL", "").rstrip("/")
    if not base or not job.video_path or not job.script:
        print("GAP: CDN_PUBLIC_BASE_URL (public https base for the rendered video) is not set, "
              "so no public media_url can be built; the publish gate would block this anyway.")
        return 3
    brand = load_brand(job.channel, settings.brand_dir)
    s = job.script
    tags = " ".join(s.hashtags)
    caption = (f"{s.description}\n{tags}".strip() or s.title)[:2000]
    req = PublishRequest(
        job_id=job.job_id, channel=job.channel, platforms=[Platform(p) for p in platforms],
        media_url=f"{base}/{job.job_id}/{Path(job.video_path).name}", title=s.title[:100],
        caption_by_platform={p: caption for p in platforms}, tags=s.hashtags,
        ai_disclosure=brand.ai_disclosure, made_for_kids=brand.made_for_kids,
        content_hash=job.content_hash or "", approved_hash=job.approved_hash or "")
    pub = BufferPublisher(dry_run=True, api_key="", channel_map={}, ledger=PostLedger(":memory:"))
    try:
        results = pub.publish(req)
    except PublishBlocked as exc:
        print("BLOCKED by publish gate:", *exc.errors, sep="\n  ")
        return 1
    for r in results:
        print(json.dumps({"platform": r.platform, "status": r.status, "dry_run": r.dry_run}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
