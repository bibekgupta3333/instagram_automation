"""Settings from the environment and the brand-pack registry (docs 04 §5.5, 13 §3)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from studio.contracts import BrandPack

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BRAND_DIR = REPO_ROOT / "brand_packs"

# Visuals. `auto` tries Pexels stock (free) -> Bedrock Stability ($/image) -> mock, per beat.
# The local mflux engines (z-image-turbo, flux2-klein-4b) stay selectable, and become the better
# default once Z-Image-Turbo (~11 GB) fits on disk (doc 14 §6).
DEFAULT_IMAGES_ENGINE = "auto"
DEFAULT_AUTO_ORDER = ("pexels", "bedrock-stability", "mock")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class VisualsConfig:
    """Env-driven knobs for the images stage (studio/stages/images.py, studio/stages/visuals/).

    STUDIO_IMAGES_AUTO_ORDER      comma list tried per beat by `auto` (default pexels,bedrock-stability,mock)
    STUDIO_STABILITY_MODEL        core | sd3.5-large | ultra | a raw Bedrock model id (default core)
    STUDIO_BEDROCK_IMAGE_REGION   Stability on Bedrock is us-west-2 (model cards, 2026-10-16)
    STUDIO_IMAGES_MAX_USD_PER_VIDEO  hard cap on paid stills per video (default $1.00)
    STUDIO_IMAGES_NEGATIVE_PROMPT override the default negative prompt
    PEXELS_API_KEY                enables the pexels engine
    STUDIO_PEXELS_CHANNELS        slugs (or `all`) whose brand style is allowed to use stock even
                                  though it looks rendered/diagrammatic
    STUDIO_ALLOW_PAID_VIDEO=1     required for bedrock-luma (Luma Ray2, $0.75-1.50/s, doc 15 §2)
    STUDIO_VIDEO_MAX_USD_PER_VIDEO  cap for paid video per video (default $4.00)
    STUDIO_LUMA_S3_URI            s3://bucket/prefix/ for Ray2 async output (us-west-2 bucket)
    """

    auto_order: tuple[str, ...] = DEFAULT_AUTO_ORDER
    stability_model: str = "core"
    bedrock_image_region: str = "us-west-2"
    max_image_usd_per_video: float = 1.00
    negative_prompt: str = (
        "text, letters, words, watermark, logo, signature, caption, blurry, low quality, "
        "deformed hands, extra fingers, distorted face"
    )
    pexels_api_key: str = ""
    pexels_channels: tuple[str, ...] = ()
    allow_paid_video: bool = False
    max_video_usd_per_video: float = 4.00
    luma_s3_uri: str = ""
    luma_region: str = "us-west-2"

    @classmethod
    def from_env(cls) -> VisualsConfig:
        d = cls()
        order = os.environ.get("STUDIO_IMAGES_AUTO_ORDER", "")
        chans = os.environ.get("STUDIO_PEXELS_CHANNELS", "")
        return cls(
            auto_order=tuple(x.strip() for x in order.split(",") if x.strip()) or d.auto_order,
            stability_model=os.environ.get("STUDIO_STABILITY_MODEL", "").strip()
            or d.stability_model,
            bedrock_image_region=os.environ.get("STUDIO_BEDROCK_IMAGE_REGION", "").strip()
            or d.bedrock_image_region,
            max_image_usd_per_video=_env_float(
                "STUDIO_IMAGES_MAX_USD_PER_VIDEO", d.max_image_usd_per_video
            ),
            negative_prompt=os.environ.get("STUDIO_IMAGES_NEGATIVE_PROMPT", d.negative_prompt),
            pexels_api_key=os.environ.get("PEXELS_API_KEY", "").strip(),
            pexels_channels=tuple(x.strip() for x in chans.split(",") if x.strip()),
            allow_paid_video=os.environ.get("STUDIO_ALLOW_PAID_VIDEO", "") == "1",
            max_video_usd_per_video=_env_float(
                "STUDIO_VIDEO_MAX_USD_PER_VIDEO", d.max_video_usd_per_video
            ),
            luma_s3_uri=os.environ.get("STUDIO_LUMA_S3_URI", "").strip(),
            luma_region=os.environ.get("STUDIO_LUMA_REGION", "").strip() or d.luma_region,
        )


@dataclass(frozen=True)
class Settings:
    output_dir: Path = Path("output")
    ollama_base_url: str = "http://localhost:11434"
    brand_dir: Path = DEFAULT_BRAND_DIR
    # Default engines when not in --mock mode. Override per stage with
    # STUDIO_TTS_ENGINE / STUDIO_CAPTIONS_ENGINE / STUDIO_IMAGES_ENGINE.
    engines: dict[str, str] = field(
        default_factory=lambda: {"tts": "kokoro", "captions": "parakeet", "images": DEFAULT_IMAGES_ENGINE}
    )

    @classmethod
    def from_env(cls) -> Settings:
        engines = {
            "tts": os.environ.get("STUDIO_TTS_ENGINE", "kokoro"),
            "captions": os.environ.get("STUDIO_CAPTIONS_ENGINE", "parakeet"),
            "images": os.environ.get("STUDIO_IMAGES_ENGINE", DEFAULT_IMAGES_ENGINE),
        }
        return cls(
            output_dir=Path(os.environ.get("STUDIO_OUTPUT_DIR", "./output")).expanduser().resolve(),
            ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            brand_dir=Path(os.environ.get("STUDIO_BRAND_DIR", str(DEFAULT_BRAND_DIR))),
            engines=engines,
        )


def get_settings() -> Settings:
    return Settings.from_env()


class BrandPackError(ValueError):
    pass


def load_brand_file(path: Path) -> BrandPack:
    data = yaml.safe_load(path.read_text()) or {}
    unknown = set(data) - set(BrandPack.model_fields)
    if unknown:
        raise BrandPackError(f"{path.name}: unknown keys {sorted(unknown)}")
    data.setdefault("slug", path.stem)
    if data["slug"] != path.stem:
        raise BrandPackError(f"{path.name}: slug {data['slug']!r} must match the file name")
    try:
        return BrandPack.model_validate(data)
    except ValidationError as exc:
        raise BrandPackError(f"{path.name}: {exc}") from exc


@lru_cache(maxsize=4)
def _load_all(brand_dir: str) -> dict[str, BrandPack]:
    packs = {}
    for path in sorted(Path(brand_dir).glob("*.yaml")):
        pack = load_brand_file(path)
        packs[pack.slug] = pack
    return packs


def load_brands(brand_dir: Path | None = None) -> dict[str, BrandPack]:
    return dict(_load_all(str(brand_dir or get_settings().brand_dir)))


def load_brand(slug: str, brand_dir: Path | None = None) -> BrandPack:
    packs = load_brands(brand_dir)
    if slug not in packs:
        raise BrandPackError(f"unknown channel {slug!r}; known: {', '.join(sorted(packs))}")
    return packs[slug]
