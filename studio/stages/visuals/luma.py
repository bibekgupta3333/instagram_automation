"""Luma Ray2 hero clip on Bedrock: a STUB that is gated and never runs by default.

Verified 2026-10-16 (Bedrock user guide, "Luma AI models" + Ray2 model card): model id
``luma.ray-v2:0``, us-west-2, async only (StartAsyncInvoke -> GetAsyncInvoke; the MP4 lands
in the S3 prefix you pass). modelInput: ``prompt`` (1-5,000 chars, <=300 tokens),
``aspect_ratio`` (1:1, 16:9, 9:16, 4:3, 3:4, 21:9, 9:21), ``loop``, ``duration`` ("5s" | "9s"),
``resolution`` ("540p" | "720p"); optional ``keyframes`` for image-to-video.
Price: $0.75/s at 540p, $1.50/s at 720p (doc 15 §2; not confirmed on the script-rendered
pricing page). Quota: 1 concurrent request (doc 15).

Gates, in order: STUDIO_ALLOW_PAID_VIDEO=1, then the estimated cost must be within
STUDIO_VIDEO_MAX_USD_PER_VIDEO, then STUDIO_LUMA_S3_URI must name an s3:// prefix in a
us-west-2 bucket. Even with all three, the S3 download leg isn't built yet:
``generate`` raises NotImplementedError after building the request, so nothing is ever spent.
"""

from __future__ import annotations

from typing import Any

from studio.config import VisualsConfig
from studio.stages.base import StageError
from studio.stages.visuals.common import BeatSpec

MODEL_ID = "luma.ray-v2:0"
USD_PER_SECOND = {"540p": 0.75, "720p": 1.50}
DURATIONS = {"5s": 5, "9s": 9}


class PaidVideoBlocked(StageError):
    """Paid video was requested but the opt-in or the $ cap forbids it."""


def estimate_usd(duration: str = "5s", resolution: str = "720p") -> float:
    return DURATIONS[duration] * USD_PER_SECOND[resolution]


def check_gate(cfg: VisualsConfig, duration: str = "5s", resolution: str = "720p") -> float:
    if not cfg.allow_paid_video:
        raise PaidVideoBlocked(
            "bedrock-luma is paid video ($0.75-1.50/s). Set STUDIO_ALLOW_PAID_VIDEO=1 to opt in."
        )
    usd = estimate_usd(duration, resolution)
    if usd > cfg.max_video_usd_per_video:
        raise PaidVideoBlocked(
            f"bedrock-luma: {duration} at {resolution} is ~${usd:.2f}, over the per-video cap "
            f"STUDIO_VIDEO_MAX_USD_PER_VIDEO=${cfg.max_video_usd_per_video:.2f}"
        )
    return usd


def build_request(spec: BeatSpec, s3_uri: str, *, duration: str = "5s",
                  resolution: str = "720p") -> dict[str, Any]:
    return {
        "modelId": MODEL_ID,
        "modelInput": {
            "prompt": spec.prompt[:5000],
            "aspect_ratio": spec.aspect_ratio,
            "loop": False,
            "duration": duration,
            "resolution": resolution,
        },
        "outputDataConfig": {"s3OutputDataConfig": {"s3Uri": s3_uri}},
    }


def generate(spec: BeatSpec, cfg: VisualsConfig, *, duration: str = "5s",
             resolution: str = "720p", client: Any | None = None):
    check_gate(cfg, duration, resolution)
    if not cfg.luma_s3_uri.startswith("s3://"):
        raise NotImplementedError(
            "bedrock-luma: no S3 output configured. Ray2 only runs async and writes the MP4 to "
            "S3. To enable it: (1) create a us-west-2 bucket the Bedrock key can write to, "
            "(2) set STUDIO_LUMA_S3_URI=s3://bucket/prefix/, (3) finish this stub: "
            "start_async_invoke(**build_request(...)) -> poll get_async_invoke(invocationArn) "
            "until status == 'Completed' (1 concurrent job) -> download "
            "<s3Uri>/<job>/output.mp4 to images/beat_NN.mp4 -> record_cost(...)."
        )
    build_request(spec, cfg.luma_s3_uri, duration=duration, resolution=resolution)
    raise NotImplementedError(
        "bedrock-luma: the async poll and S3 download are not implemented yet (stub). The "
        "request body is ready (build_request); no call was made and nothing was spent."
    )
