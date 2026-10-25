"""Optional offline character providers, invoked inside the supervised worker.

Capability checks never import ML runtimes or claim a verified CUDA installation.
Bedrock remains text-only and is deliberately absent from this module.
"""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

from studio.web.renderer import character_preview, stop_process

KLEIN_REPO = "mflux-community/flux2-klein-4b-mflux-q4"


def offline_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                "DO_NOT_TRACK": "1", "OMP_NUM_THREADS": "3", "MKL_NUM_THREADS": "3",
                "OPENBLAS_NUM_THREADS": "3", "NUMEXPR_NUM_THREADS": "3",
                "TOKENIZERS_PARALLELISM": "false", "STUDIO_ALLOW_MODEL_DOWNLOAD": "0"})
    return env


def _mflux_executable() -> str | None:
    configured = os.environ.get("STUDIO_WEB_MFLUX_BIN", "")
    if configured:
        executable = Path(configured)
        return str(executable) if executable.is_absolute() and executable.is_file() and os.access(
            executable, os.X_OK) else None
    repo = Path(__file__).resolve().parents[2]
    for executable in (Path(sys.executable).parent / "mflux-generate-flux2",
                       repo / ".venv/bin/mflux-generate-flux2"):
        if executable.is_file() and os.access(executable, os.X_OK):
            return str(executable)
    return shutil.which("mflux-generate-flux2")


def cached_klein() -> Path | None:
    cache = Path(os.environ.get("HF_HUB_CACHE", str(
        Path(os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface"))) / "hub")))
    root = cache / ("models--" + KLEIN_REPO.replace("/", "--"))
    try:
        revision = (root / "refs/main").read_text(encoding="utf-8").strip()
        if not revision or any(c not in "0123456789abcdef" for c in revision):
            return None
        snapshot = root / "snapshots" / revision
        # mflux q4 uses component shard indexes, not a top-level config.json.
        for component in ("transformer", "text_encoder", "vae"):
            directory = snapshot / component
            index = json.loads((directory / "model.safetensors.index.json").read_text())
            shards = set(index["weight_map"].values())
            if not shards:
                return None
            for shard in shards:
                if (not isinstance(shard, str) or Path(shard).name != shard
                        or not shard.endswith(".safetensors")):
                    return None
                weight = directory / shard
                if not weight.is_file() or weight.stat().st_size == 0:
                    return None
        for filename in ("tokenizer.json", "tokenizer_config.json"):
            config = json.loads((snapshot / "tokenizer" / filename).read_text())
            if not isinstance(config, dict):
                return None
        if any(not p.is_file() for p in snapshot.rglob("*.safetensors")):
            return None
        return snapshot
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _diffusers_model() -> Path | None:
    configured = os.environ.get("STUDIO_WEB_DIFFUSERS_MODEL", "")
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute() or not path.is_dir() or not (path / "model_index.json").is_file():
        return None
    return path


def capabilities() -> list[dict]:
    mac = platform.system() == "Darwin"
    mflux = mac and _mflux_executable() is not None and cached_klein() is not None
    diffusers = (platform.system() == "Linux" and _diffusers_model() is not None
                 and importlib.util.find_spec("diffusers") is not None
                 and importlib.util.find_spec("torch") is not None)
    return [
        {"id": "illustration", "label": "Original local illustration", "available": True,
         "reason": "Model-free Pillow drawing"},
        {"id": "mflux", "label": "Cached FLUX.2 klein q4 (Mac)", "available": mflux,
         "reason": "Cached q4 model and CLI found; inference unverified" if mflux else
         "Requires macOS, installed mflux CLI and cached klein q4 weights; no downloads"},
        {"id": "diffusers", "label": "Local FLUX.2 klein (Linux CUDA)", "available": diffusers,
         "reason": "Local model and libraries found; CUDA and inference unverified" if diffusers
         else "Requires Linux, optional torch/diffusers and STUDIO_WEB_DIFFUSERS_MODEL local directory"},
    ]


# Public compatibility name used by the API and external integrations.
available_providers = capabilities


def _prompt(character: dict) -> str:
    # Only describe our geometric animal; do not pass arbitrary user prompt instructions.
    character_preview(character)  # validate species/colour/accessory
    return (f"Original friendly {character.get('species', 'otter')} character, "
            f"main colour {character.get('color', '#B88054')}, "
            f"accessory {character.get('accessory', 'none')}, "
            "rounded felt-paper shapes, small oval eyes, gentle smile, full body, "
            "centred, plain cream background, original pond storybook illustration. "
            "No text, no letters, no logos, no watermark, no existing franchise characters.")


def mflux_command(character: dict, output: Path) -> list[str]:
    executable, model = _mflux_executable(), cached_klein()
    if platform.system() != "Darwin" or not executable or model is None:
        raise RuntimeError("mflux requires macOS and an installed, locally cached klein q4 model")
    return [executable, "--model", str(model), "--base-model", "flux2-klein-4b",
            "--prompt", _prompt(character), "--width", "512", "--height", "512",
            "--steps", "4", "--guidance", "1.0", "--seed", "17", "--low-ram",
            "--vae-tile-size", "256", "--mlx-cache-limit-gb", "0.25", "--output", str(output)]


def _run(command: list[str], out: Path) -> None:
    with (out / "provider.log").open("wb") as log:
        process = subprocess.Popen(command, env=offline_environment(),
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            if process.wait(timeout=600) != 0:
                raise RuntimeError("Character provider failed; see provider.log")
        finally:
            stop_process(process)


def generate_character(character: dict, provider: str, out: Path) -> dict:
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    output = out / "character.png"
    if provider == "illustration":
        output.write_bytes(character_preview(character))
    elif provider == "mflux":
        _run(mflux_command(character, output), out)
    elif provider == "diffusers":
        if platform.system() != "Linux" or _diffusers_model() is None:
            raise RuntimeError("Diffusers requires a configured local Linux model directory")
        # Keep ML allocation in a separate child under the existing supervisor.
        snapshot = out / "provider-character.json"
        snapshot.write_text(json.dumps(character), encoding="utf-8")
        _run([sys.executable, "-m", "studio.web.providers", "--diffusers-character", str(snapshot),
              "--output", str(output)], out)
    else:
        raise ValueError("Unsupported character provider")
    with Image.open(output) as image:
        if image.size != (512, 512):
            raise RuntimeError("Character provider must return a 512 square image")
        converted = image.convert("RGB")
    converted.save(output, format="PNG")
    return {"kind": "character", "provider": provider, "guide_audio": True, "seconds": 0,
            "width": 512, "height": 512, "made_for_kids": True, "ai_disclosure": True,
            "approved": False, "review_ready": False}


def _diffusers_generate(character: dict, output: Path) -> None:
    """Lazy optional CUDA path. Existence checks do not constitute CUDA verification."""
    model = _diffusers_model()
    if platform.system() != "Linux" or model is None:
        raise RuntimeError("A local Diffusers model directory on Linux is required")
    os.environ.update(offline_environment())
    import torch
    from diffusers import Flux2KleinPipeline

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; select the illustration provider")
    torch.set_num_threads(3)
    pipeline = None
    try:
        pipeline = Flux2KleinPipeline.from_pretrained(
            str(model), local_files_only=True, torch_dtype=torch.bfloat16)
        pipeline.enable_model_cpu_offload()
        pipeline.vae.enable_tiling()
        with torch.inference_mode():
            image = pipeline(prompt=_prompt(character), width=512, height=512,
                             num_inference_steps=4, guidance_scale=1.0,
                             generator=torch.Generator(device="cpu").manual_seed(17)).images[0]
        image.save(output)
    finally:
        del pipeline
        torch.cuda.empty_cache()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--diffusers-character", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _diffusers_generate(json.loads(args.diffusers_character.read_text()), args.output)
