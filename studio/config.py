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
# skipped unless downloaded) -> Pixabay stock (free) -> mock. Motion is local parallax (render).
# flux2-klein-4b stays selectable with --engine images=flux2-klein-4b once downloaded.
# Bedrock is for TEXT models only: the bedrock-stability / bedrock-luma engines were removed.
DEFAULT_IMAGES_ENGINE = "auto"
DEFAULT_AUTO_ORDER = ("z-image-turbo", "pixabay", "mock")
# Stock-first channels: real-world subjects (cars, houses, companies) where real Pixabay footage
# looks far better than an AI still. A beat goes stock-first only when the script gave it a
# `stock_query` and its shot isn't a diagram/cutaway (studio/stages/images.py); every other beat
# of these channels, and every beat of the other channels, uses DEFAULT_AUTO_ORDER.
STOCK_FIRST_ORDER = ("pixabay", "z-image-turbo", "mock")
STOCK_FIRST_CHANNELS = ("car-cost-truth", "square-foot-money", "business-model-files")
REMOVED_IMAGE_ENGINES = frozenset({"bedrock-stability", "bedrock-luma"})


class RemovedEngineError(ValueError):
    """An images engine name that no longer exists (see REMOVED_IMAGE_ENGINES)."""


def check_image_engine(name: str, where: str) -> None:
    """Raise RemovedEngineError if `name` is a removed images engine (never silently skip)."""
    if name in REMOVED_IMAGE_ENGINES:
        raise RemovedEngineError(
            f"{where}: images engine {name!r} was removed 2026-10-16: Bedrock is text-only; "
            "use z-image-turbo or pixabay"
        )

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
        if value.startswith("#"):  # `KEY=   # comment` is an empty value, not "# comment"
            value = ""
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
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


@dataclass(frozen=True)
class VisualsConfig:
    """Env-driven knobs for the images stage (studio/stages/images.py, studio/stages/visuals/).

    STUDIO_IMAGES_AUTO_ORDER      comma list tried per beat by `auto`
                                  (default z-image-turbo,pixabay,mock; local mflux is skipped
                                  unless its pre-quantized model is already downloaded).
                                  bedrock-stability / bedrock-luma raise RemovedEngineError.
    PIXABAY_API_KEY               enables the pixabay engine (free stock, visuals/pixabay.py)
    STUDIO_PIXABAY_CHANNELS       slugs (or `all`) whose brand style is allowed to use stock even
                                  though it looks rendered/diagrammatic
    STUDIO_STOCK_FIRST_CHANNELS   slugs (or `none`) that try Pixabay first for beats with a
                                  stock_query (default STOCK_FIRST_CHANNELS)
    STUDIO_ZIMAGE_STEPS           Z-Image-Turbo denoising steps (default 9)
    STUDIO_ZIMAGE_SIZE            portrait generation size WxH, multiples of 16 (default 640x1152;
                                  long-form uses the transpose). Measured 2026-10-17 on the M4 Pro,
                                  then Real-ESRGAN to 1080x1920: 576x1024 60 s (waxy, painterly
                                  skin/texture after x4), 640x1152 76-83 s (+27%, visibly finer
                                  texture), 720x1280 100-112 s (+67%, no further visible gain);
                                  12 steps instead of 9 cost +30% and looked the same.

    An explicit STUDIO_IMAGES_AUTO_ORDER overrides the per-brand visual order for every beat.
    """

    auto_order: tuple[str, ...] = DEFAULT_AUTO_ORDER
    pixabay_api_key: str = ""
    pixabay_channels: tuple[str, ...] = ()
    stock_first_channels: tuple[str, ...] = STOCK_FIRST_CHANNELS
    order_overridden: bool = False  # STUDIO_IMAGES_AUTO_ORDER was set
    zimage_steps: int = 9
    zimage_size: tuple[int, int] = (640, 1152)

    def visual_order(self, slug: str, stock_beat: bool = True) -> tuple[str, ...]:
        """The `auto` engine order for one beat of channel `slug`. `stock_beat` says whether
        the beat suits stock (has a stock_query and isn't a diagram/cutaway shot)."""
        if self.order_overridden:
            return self.auto_order
        if stock_beat and slug in self.stock_first_channels:
            return STOCK_FIRST_ORDER
        return self.auto_order

    def is_stock_first(self, slug: str) -> bool:
        return not self.order_overridden and slug in self.stock_first_channels

    @classmethod
    def from_env(cls) -> VisualsConfig:
        load_dotenv_once()
        d = cls()
        order = tuple(x.strip() for x in os.environ.get("STUDIO_IMAGES_AUTO_ORDER", "").split(",")
                      if x.strip())
        for name in order:
            check_image_engine(name, "STUDIO_IMAGES_AUTO_ORDER")
        chans = os.environ.get("STUDIO_PIXABAY_CHANNELS", "")
        first = os.environ.get("STUDIO_STOCK_FIRST_CHANNELS")
        if first is None:
            stock_first = d.stock_first_channels
        elif first.strip().lower() in _ENV_DISABLED:
            stock_first = ()
        else:
            stock_first = tuple(x.strip() for x in first.split(",") if x.strip())
        return cls(
            auto_order=order or d.auto_order,
            pixabay_api_key=os.environ.get("PIXABAY_API_KEY", "").strip(),
            pixabay_channels=tuple(x.strip() for x in chans.split(",") if x.strip()),
            stock_first_channels=stock_first,
            order_overridden=bool(order),
            zimage_steps=_int_env("STUDIO_ZIMAGE_STEPS", d.zimage_steps, 1, 30),
            zimage_size=_size_env("STUDIO_ZIMAGE_SIZE", d.zimage_size),
        )


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not an integer") from exc
    if not lo <= value <= hi:
        raise ValueError(f"{name}={value} is outside {lo}..{hi}")
    return value


def _size_env(name: str, default: tuple[int, int]) -> tuple[int, int]:
    """WxH, both multiples of 16 (mflux) and between 256 and 2048."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    try:
        w, h = (int(x) for x in raw.split("x"))
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r}: use WxH, e.g. 576x1024") from exc
    if w % 16 or h % 16 or not (256 <= w <= 2048 and 256 <= h <= 2048):
        raise ValueError(f"{name}={raw!r}: sides must be multiples of 16 in 256..2048")
    return (w, h)


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
