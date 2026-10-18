"""Offline Wan VACE reference-to-video pilot. Run through scripts/run_guarded.py.

This is a standalone experimental generator, not a publishing pipeline stage.
Weights must already have been installed with the named `make models` selection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import time
from pathlib import Path

# Exact files and upstream LFS digests checked 2026-10-18. No runtime downloads.
COMPONENTS = (
    ("calcuis/wan-1.3b-gguf", "wan2.1-v3-vace-1.3b-q4_0.gguf",
     "0652f175f44055eb60cca26dd7cd89c14abe22ce",
     "c9d2ad33427cef0e485c0c32b105c3cca8a33f6bfd8249b41b9685756aed4a74"),
    ("city96/umt5-xxl-encoder-gguf", "umt5-xxl-encoder-Q3_K_S.gguf",
     "b535255bee98c2b0a59ea7c0ae2dcd0c6657b3b7",
     "f64f8d6dc4d8a24276df69d0ccea789aae686f7417950a41e6568c30cb478a5c"),
    ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/vae/wan_2.1_vae.safetensors",
     "123acf1cc74bccbb9bfff8ac1ee72edc08c2341d",
     "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b"),
)
TINY_DECODER = (
    "Kijai/WanVideo_comfy", "taew2_1.safetensors",
    "8260d429d19fd7a72304cad059160b95d843913f",
    "a870ebc6a8bc36bb4124c304341e7b258ab4010e365743fbac2dc43af98cf9b2",
)


def cached_components(decoder: str = "full") -> list[Path]:
    from huggingface_hub import try_to_load_from_cache

    paths = []
    components = COMPONENTS if decoder == "full" else (*COMPONENTS[:2], TINY_DECODER)
    for repo, filename, revision, digest in components:
        cached = try_to_load_from_cache(repo, filename, revision=revision)
        if not isinstance(cached, str) or not Path(cached).is_file():
            raise RuntimeError(f"Missing cached component {repo}/{filename}; run make models first")
        path = Path(cached)
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise RuntimeError(f"Checksum mismatch: {repo}/{filename}")
        paths.append(path)
    return paths


def command(args: argparse.Namespace, paths: list[Path]) -> list[str]:
    diffusion, encoder, vae = paths
    return [
        str(args.runtime.resolve()), "-M", "vid_gen",
        "--diffusion-model", str(diffusion), "--t5xxl", str(encoder),
        "--taesd" if getattr(args, "decoder", "full") == "tiny" else "--vae", str(vae),
        "-i", str(args.reference.resolve()), "-p", args.prompt,
        "-n", ("text, captions, watermark, logo, blurry, distorted face, deformed eyes, "
               "extra limbs, rapid movement, camera shake"),
        "-W", str(args.width), "-H", str(args.height), "--video-frames", str(args.frames),
        "--fps", "16", "--steps", str(args.steps), "--cfg-scale", "6",
        "--flow-shift", "3", "--sampling-method", "euler", "-s", str(args.seed),
        "--threads", "3", "--mmap", "--offload-to-cpu", "--flash-attn", "on",
        "--batched-cfg", "off", "--vae-tiling", "--vae-tile-size", "128x128",
        "--temporal-tiling", "--disable-prefetch", "--max-vram", "6",
        "-o", str((args.output_dir / "raw.avi").resolve()),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=384)
    parser.add_argument("--frames", type=int, default=17)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--seed", type=int, default=18042)
    parser.add_argument("--decoder", choices=("full", "tiny"), default="full")
    parser.add_argument("--prompt", default=(
        "A cinematic close-up of the fictional adult woman in the reference image. "
        "She gently turns her head a little, blinks naturally, and gives a subtle friendly smile. "
        "Realistic skin, soft window lighting, purple jacket, calm teal studio background. "
        "Fixed camera, subtle natural human motion. No text or logos."
    ))
    args = parser.parse_args()
    if not (16 <= args.width <= 512 and 16 <= args.height <= 512):
        parser.error("Pilot dimensions must be 16..512 pixels")
    if args.width % 16 or args.height % 16:
        parser.error("Dimensions must be multiples of 16")
    if not (5 <= args.frames <= 33 and (args.frames - 1) % 4 == 0):
        parser.error("Pilot frames must be 5..33, in 4n+1 increments")
    if not 1 <= args.steps <= 40:
        parser.error("Steps must be 1..40")
    if not args.runtime.is_file() or not args.reference.is_file():
        parser.error("Runtime and reference image must already exist")
    if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("STUDIO_CPU_THREADS") != "3":
        parser.error("Run this pilot through scripts/run_guarded.py")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if (args.output_dir / "raw.avi").exists() or (args.output_dir / "video.mp4").exists():
        parser.error("Use a fresh output directory; existing media will not be overwritten")
    paths = cached_components(args.decoder)
    cmd = command(args, paths)
    report = {"model": "Wan2.1 VACE 1.3B Q4_0", "decoder": args.decoder, "command": cmd,
              "weight_bytes": sum(p.stat().st_size for p in paths),
              "audio_driven_lip_sync": False, "approved": False, "published": False}
    (args.output_dir / "run.json").write_text(json.dumps(report, indent=2))
    start = time.monotonic()
    with (args.output_dir / "generation.log").open("w") as log:
        result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False)
    report.update(returncode=result.returncode, generation_seconds=round(time.monotonic()-start, 2),
                  child_max_rss_bytes=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                  rss_caveat="macOS process RSS, not total shared CPU/GPU memory")
    (args.output_dir / "run.json").write_text(json.dumps(report, indent=2))
    if result.returncode:
        print(f"Generation failed ({result.returncode}); see {args.output_dir / 'generation.log'}")
        return 1
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-threads", "3",
        "-i", str(args.output_dir / "raw.avi"), "-an", "-c:v", "h264_videotoolbox",
        "-b:v", "3M", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(args.output_dir / "video.mp4"),
    ], check=True)
    print(f"Unapproved neural-video pilot: {args.output_dir / 'video.mp4'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
