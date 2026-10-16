"""Stability AI text-to-image on Amazon Bedrock (InvokeModel, us-west-2).

Verified 2026-10-16 against the Bedrock user guide (model cards + request/response pages):
- ``stability.stable-image-core-v1:1`` (Stable Image Core), ``stability.sd3-5-large-v1:0``
  (SD3.5 Large), ``stability.stable-image-ultra-v1:1`` (Stable Image Ultra): Active, In-Region
  us-west-2 only, InvokeModel only (no Converse, no streaming).
- Body: ``prompt`` (<=10k chars), ``aspect_ratio`` (16:9, 1:1, 21:9, 2:3, 3:2, 4:5, 5:4, 9:16,
  9:21), ``output_format`` (png/jpeg; SD3.5 also webp), ``seed`` (0 = random; 0..4294967294),
  ``negative_prompt``. Response: ``{"seeds": [...], "finish_reasons": [null], "images": [b64]}``;
  a non-null finish reason ("Filter reason: prompt", ...) means no image.
- Prices (USD per image): Core $0.04 (AWS Marketplace listing), SD3.5 Large $0.08 and Ultra
  $0.14 (doc 15 §2; the AWS pricing page is script-rendered, so treat these two as unverified).
- Auth: boto3 honours ``AWS_BEARER_TOKEN_BEDROCK`` (Bedrock API key) natively.
"""

from __future__ import annotations

import base64
import json
import os
import random
import time
from collections.abc import Callable
from typing import Any

from studio.stages.base import EngineUnavailable, StageError
from studio.stages.visuals.common import BeatSpec, Visual, logger

MODELS: dict[str, tuple[str, float]] = {
    # alias -> (Bedrock model id, USD per image)
    "core": ("stability.stable-image-core-v1:1", 0.04),
    "sd3.5-large": ("stability.sd3-5-large-v1:0", 0.08),
    "ultra": ("stability.stable-image-ultra-v1:1", 0.14),
}
DEFAULT_PRICE = 0.14  # unknown raw id: assume the dearest
RETRYABLE = frozenset({
    "ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException",
    "InternalServerException", "ModelTimeoutException", "ModelNotReadyException",
})
UNAVAILABLE = frozenset({
    "AccessDeniedException", "UnrecognizedClientException", "ResourceNotFoundException",
    "ExpiredTokenException", "InvalidSignatureException",
})
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def resolve_model(name: str) -> tuple[str, float]:
    if name in MODELS:
        return MODELS[name]
    for model_id, price in MODELS.values():
        if name == model_id:
            return model_id, price
    return name, DEFAULT_PRICE


def build_body(spec: BeatSpec, *, negative_prompt: str = "", output_format: str = "png") -> dict:
    body: dict[str, Any] = {
        "prompt": spec.prompt[:10_000],
        "aspect_ratio": spec.aspect_ratio,
        "output_format": output_format,
        "seed": spec.seed,
    }
    if negative_prompt:
        body["negative_prompt"] = negative_prompt[:10_000]
    return body


def has_credentials() -> bool:
    """Only a Bedrock API key or an explicit AWS_PROFILE counts. We never fall through to the
    default chain, because the profiles on this machine belong to the user's employer."""
    return bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or os.environ.get("AWS_PROFILE"))


def runtime_client(region: str):
    if not has_credentials():
        raise EngineUnavailable(
            "bedrock-stability needs AWS_BEARER_TOKEN_BEDROCK (a Bedrock API key) or AWS_PROFILE "
            "in .env; neither is set."
        )
    try:  # shared auth helper from the LLM layer (studio/llm/bedrock.py), if present
        from studio.llm import bedrock as llm_bedrock

        for name in ("runtime_client", "get_runtime_client", "bedrock_runtime_client",
                     "_session_client"):
            fn = getattr(llm_bedrock, name, None)
            if callable(fn):
                try:
                    return fn(region=region)
                except TypeError:
                    continue
    except ImportError:
        pass
    try:
        import boto3  # type: ignore[import-not-found]
        from botocore.config import Config  # type: ignore[import-not-found]
    except ImportError as exc:
        raise EngineUnavailable("bedrock-stability needs boto3 (`uv sync --extra bedrock`)") from exc
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        # botocore >= 1.39 reads the key itself; cut the session off from ~/.aws profiles.
        import botocore.session  # type: ignore[import-not-found]

        isolated = botocore.session.Session(session_vars={
            "profile": (None, None, None, None),
            "config_file": (None, None, os.devnull, None),
            "credentials_file": (None, None, os.devnull, None),
        })
        session = boto3.Session(botocore_session=isolated, region_name=region)
    else:
        session = boto3.Session(profile_name=os.environ["AWS_PROFILE"], region_name=region)
    return session.client(
        "bedrock-runtime",
        config=Config(retries={"mode": "standard", "max_attempts": 2}, read_timeout=120),
    )


def _error_code(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str(resp.get("Error", {}).get("Code", ""))
    return ""


def decode_image(payload: dict[str, Any]) -> bytes:
    reasons = payload.get("finish_reasons") or [None]
    if reasons[0] is not None:
        raise StageError(f"bedrock-stability: no image ({reasons[0]})")
    images = payload.get("images") or []
    if not images:
        raise StageError("bedrock-stability: response had no images")
    data = base64.b64decode(images[0])
    if not data.startswith((PNG_MAGIC, JPEG_MAGIC)):
        raise StageError("bedrock-stability: decoded payload is not a PNG/JPEG")
    return data


def generate(
    spec: BeatSpec,
    *,
    model: str = "core",
    region: str = "us-west-2",
    negative_prompt: str = "",
    client: Any | None = None,
    attempts: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> Visual:
    model_id, price = resolve_model(model)
    rt = client if client is not None else runtime_client(region)
    body = json.dumps(build_body(spec, negative_prompt=negative_prompt))
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = rt.invoke_model(modelId=model_id, body=body, contentType="application/json",
                                   accept="application/json")
            raw = resp["body"].read() if hasattr(resp["body"], "read") else resp["body"]
            data = decode_image(json.loads(raw))
            break
        except StageError:
            raise
        except Exception as exc:  # botocore ClientError / connection errors
            code = _error_code(exc)
            if code in UNAVAILABLE:
                raise EngineUnavailable(f"bedrock-stability: {code}: {exc}") from exc
            transient = code in RETRYABLE or not code  # no code: network/timeout
            if not transient or attempt == attempts - 1:
                raise StageError(f"bedrock-stability failed ({code or type(exc).__name__}): "
                                 f"{exc}") from exc
            last = exc
            delay = min(30.0, 2.0 * 2**attempt) * (0.75 + random.random() / 2)
            logger.warning("bedrock-stability %s, retry %d in %.1fs", code or exc, attempt + 1,
                           delay)
            sleep(delay)
    else:  # pragma: no cover - loop always breaks or raises
        raise StageError(f"bedrock-stability failed: {last}")
    suffix = ".png" if data.startswith(PNG_MAGIC) else ".jpg"
    out = spec.out_stem.with_suffix(suffix)
    tmp = out.with_suffix(suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(out)
    return Visual(path=out, kind="image", engine="bedrock-stability", model=model_id, usd=price,
                  source={"seed": spec.seed, "aspect_ratio": spec.aspect_ratio})
