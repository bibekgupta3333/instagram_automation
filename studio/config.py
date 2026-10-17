"""Settings from the environment and the brand-pack registry (docs 04 §5.5, 13 §3)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from studio.contracts import BrandPack

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BRAND_DIR = REPO_ROOT / "brand_packs"

# Visuals (Configuration A, doc 18 §1). `auto` tries, per beat: local Z-Image-Turbo 4-bit (free,
# skipped unless downloaded) -> Pixabay stock (free) -> Bedrock Stability ($/image) -> mock.
# flux2-klein-4b stays selectable with --engine images=flux2-klein-4b once downloaded.
DEFAULT_IMAGES_ENGINE = "auto"
DEFAULT_AUTO_ORDER = ("z-image-turbo", "pixabay", "bedrock-stability", "mock")

# ---------------------------------------------------------------- .env loading
# Dependency-free: KEY=VALUE lines, `export KEY=VALUE` allowed, `#` comments and blank lines
# ignored, matching single/double quotes stripped, empty values skipped. Variables already in
# the environment always win. The file is the repo-root `.env`, or STUDIO_ENV_FILE (set it to
# `none` to disable). Under pytest the repo `.env` is NOT loaded unless STUDIO_ENV_FILE is set,
# so the offline suite never picks up real keys.
_ENV_DISABLED = {"", "0", "none", "off", "false"}
_loaded_env_files: set[str] = set()


def parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # inline comment on an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def load_env_file(path: Path | str) -> list[str]:
    """Set variables from `path` that are not already set. Returns the keys it set (never the
    values). A missing file is not an error."""
    try:
        text = Path(path).read_text()
    except OSError:
        return []
    set_keys = []
    for key, value in parse_env_text(text).items():
        if value and key not in os.environ:
            os.environ[key] = value
            set_keys.append(key)
    return set_keys


def env_file_path() -> Path | None:
    raw = os.environ.get("STUDIO_ENV_FILE")
    if raw is not None:
        return None if raw.strip().lower() in _ENV_DISABLED else Path(raw).expanduser()
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
        return None
    return REPO_ROOT / ".env"


def load_dotenv_once() -> list[str]:
    """Load the .env file once per process (per path)."""
    path = env_file_path()
    if path is None:
        return []
    marker = str(path.resolve())
    if marker in _loaded_env_files:
        return []
    _loaded_env_files.add(marker)
    return load_env_file(path)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class VisualsConfig:
    """Env-driven knobs for the images stage (studio/stages/images.py, studio/stages/visuals/).

    STUDIO_IMAGES_AUTO_ORDER      comma list tried per beat by `auto`
                                  (default z-image-turbo,pixabay,bedrock-stability,mock; local mflux
                                  is skipped unless its pre-quantized model is already downloaded)
    STUDIO_STABILITY_MODEL        core | sd3.5-large | ultra | a raw Bedrock model id (default core)
    STUDIO_BEDROCK_IMAGE_REGION   Stability on Bedrock is us-west-2 (model cards, 2026-10-16)
    STUDIO_IMAGES_MAX_USD_PER_VIDEO  hard cap on paid stills per video (default $1.00)
    STUDIO_IMAGES_NEGATIVE_PROMPT override the default negative prompt
    PIXABAY_API_KEY               enables the pixabay engine (free stock, visuals/pixabay.py)
    STUDIO_PIXABAY_CHANNELS       slugs (or `all`) whose brand style is allowed to use stock even
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
    pixabay_api_key: str = ""
    pixabay_channels: tuple[str, ...] = ()
    allow_paid_video: bool = False
    max_video_usd_per_video: float = 4.00
    luma_s3_uri: str = ""
    luma_region: str = "us-west-2"

    @classmethod
    def from_env(cls) -> VisualsConfig:
        load_dotenv_once()
        d = cls()
        order = os.environ.get("STUDIO_IMAGES_AUTO_ORDER", "")
        chans = os.environ.get("STUDIO_PIXABAY_CHANNELS", "")
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
            pixabay_api_key=os.environ.get("PIXABAY_API_KEY", "").strip(),
            pixabay_channels=tuple(x.strip() for x in chans.split(",") if x.strip()),
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
        load_dotenv_once()
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
