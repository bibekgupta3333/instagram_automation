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


@dataclass(frozen=True)
class Settings:
    output_dir: Path = Path("output")
    ollama_base_url: str = "http://localhost:11434"
    brand_dir: Path = DEFAULT_BRAND_DIR
    # Default engines when not in --mock mode. Override per stage with
    # STUDIO_TTS_ENGINE / STUDIO_CAPTIONS_ENGINE / STUDIO_IMAGES_ENGINE.
    engines: dict[str, str] = field(
        default_factory=lambda: {"tts": "kokoro", "captions": "parakeet", "images": "z-image-turbo"}
    )

    @classmethod
    def from_env(cls) -> Settings:
        engines = {
            "tts": os.environ.get("STUDIO_TTS_ENGINE", "kokoro"),
            "captions": os.environ.get("STUDIO_CAPTIONS_ENGINE", "parakeet"),
            "images": os.environ.get("STUDIO_IMAGES_ENGINE", "z-image-turbo"),
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
