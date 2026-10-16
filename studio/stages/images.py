"""Stage 5: one still per beat. mflux with Z-Image-Turbo (default stills) or FLUX.2 [klein] 4B
(fast drafts), both Apache-2.0 (doc 14 §6). The mock engine renders a 1080x1920 (or 1920x1080)
placeholder with ffmpeg: a solid brand colour plus the wrapped visual prompt.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path

from studio.contracts import BrandPack, VideoJob
from studio.stages.base import (
    EngineUnavailable,
    StageContext,
    StageError,
    hash_inputs,
    is_cached,
    log,
    luminance,
    palette_roles,
    run_ffmpeg,
    sha256_bytes,
    write_cache,
)

STAGE = "images"
VERSION = 2

# engine id -> (doc 14 model name, default mflux CLI). Check `mflux --help` after installing;
# override the command with STUDIO_MFLUX_CMD_<ENGINE> (e.g. STUDIO_MFLUX_CMD_Z_IMAGE_TURBO).
MFLUX_MODELS: dict[str, tuple[str, list[str]]] = {
    "z-image-turbo": ("Z-Image-Turbo", ["mflux-generate-z-image-turbo", "--steps", "9"]),
    "flux2-klein-4b": (
        "FLUX.2 [klein] 4B",
        ["mflux-generate-flux2", "--model", "flux2-klein-4b", "--steps", "4"],
    ),
}

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def frame_size(fmt: str) -> tuple[int, int]:
    return (1080, 1920) if fmt == "short" else (1920, 1080)


def gen_size(fmt: str) -> tuple[int, int]:
    """mflux needs multiples of 16; render upscales to the frame size."""
    return (720, 1280) if fmt == "short" else (1280, 720)


def image_prompt(visual_prompt: str, brand: BrandPack) -> str:
    return f"{visual_prompt.strip().rstrip('.')}, {brand.style_prompt}".strip(", ")


def _font() -> str | None:
    return next((f for f in FONT_CANDIDATES if Path(f).exists()), None)


def _q(value: str) -> str:
    """Quote a filter option value for ffmpeg's filtergraph parser."""
    return "'" + value.replace("\\", "\\\\").replace("'", "'\\''").replace(":", "\\:") + "'"


def _mock_image(out: Path, prompt: str, index: int, total: int, brand: BrandPack, fmt: str) -> None:
    w, h = frame_size(fmt)
    pal = brand.palette or ["#222222"]
    bg = pal[index % len(pal)]
    roles = palette_roles(pal)
    fg = "#FFFFFF" if luminance(bg) < 0.55 else roles["dark"]
    accent = roles["accent"] if roles["accent"] != bg else roles["light"]
    cols = 24 if fmt == "short" else 48
    lines = textwrap.wrap(prompt, width=cols, max_lines=14 if fmt == "short" else 8,
                          placeholder=" ...")
    head = f"{brand.name.upper()}  -  BEAT {index + 1:02d}/{total:02d}  -  MOCK"
    font = _font()
    fontopt = f"fontfile={_q(font)}:" if font else ""
    fs = int(h * 0.032) if fmt == "short" else int(h * 0.045)
    step = int(fs * 1.35)
    margin = int(w * 0.08)
    # one drawtext per line, each from its own text file (no escaping; newlines in a single
    # drawtext render as missing-glyph boxes with some fonts)
    files = [(out.with_suffix(".head.txt"), head)]
    files += [(out.with_suffix(f".l{i:02d}.txt"), line) for i, line in enumerate(lines)]
    for path, text in files:
        path.write_text(text)
    top = (h - step * len(lines)) // 2
    filters = [
        (f"drawbox=x={margin // 2}:y={margin // 2}:w=iw-{margin}:h=ih-{margin}"
        f":color={accent.replace('#', '0x')}@0.9:t=10"),
        (f"drawtext={fontopt}textfile={files[0][0].name}:expansion=none:fontsize={int(fs * 0.6)}"
        f":fontcolor={accent.replace('#', '0x')}:x=(w-text_w)/2:y={int(margin * 1.2)}"),
    ]
    for i, (path, _) in enumerate(files[1:]):
        filters.append(
            f"drawtext={fontopt}textfile={path.name}:expansion=none:fontsize={fs}"
            f":fontcolor={fg.replace('#', '0x')}:x=(w-text_w)/2:y={top + i * step}"
        )
    try:
        run_ffmpeg(
            ["-f", "lavfi", "-i", f"color=c={bg.replace('#', '0x')}:s={w}x{h}:d=1",
             "-vf", ",".join(filters), "-frames:v", "1", "-update", "1", out.name],
            cwd=out.parent,
        )
    finally:
        for path, _ in files:
            path.unlink(missing_ok=True)


def _mflux_cmd(engine: str) -> list[str]:
    try:
        import mflux  # noqa: F401  (lazy presence check; generation runs via the mflux CLI)
    except ImportError as exc:
        raise EngineUnavailable(
            f"Images engine {engine!r} needs mflux, which is not installed. Install the local-ml "
            "extra (`uv sync --extra local-ml` or `uv tool install mflux`) or use --mock / "
            "--engine images=mock."
        ) from exc
    override = os.environ.get("STUDIO_MFLUX_CMD_" + engine.upper().replace("-", "_"))
    cmd = shlex.split(override) if override else list(MFLUX_MODELS[engine][1])
    if not shutil.which(cmd[0]):
        raise EngineUnavailable(f"{cmd[0]} not on PATH; set STUDIO_MFLUX_CMD_* to the mflux CLI")
    return cmd


def _mflux_image(out: Path, prompt: str, seed: int, engine: str, fmt: str) -> None:
    w, h = gen_size(fmt)
    cmd = [*_mflux_cmd(engine), "--prompt", prompt, "--width", str(w), "--height", str(h),
           "-q", "8", "--seed", str(seed), "--output", str(out)]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        raise StageError(f"mflux failed for {out.name}: {proc.stderr.strip()[-800:]}")


def unload() -> None:
    """mflux runs as a subprocess, so its memory is freed when each call exits."""


def run(job: VideoJob, brand: BrandPack, ctx: StageContext) -> VideoJob:
    if job.script is None:
        raise StageError("images: no script")
    engine = ctx.engine("images")
    if engine != "mock" and engine not in MFLUX_MODELS:
        raise StageError(f"images: unknown engine {engine!r} (mock, {', '.join(MFLUX_MODELS)})")
    img_dir = Path(job.workdir) / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    prompts = [image_prompt(b.visual_prompt, brand) for b in job.script.beats]
    outs = [img_dir / f"beat_{i:02d}.png" for i in range(len(prompts))]
    key = hash_inputs(STAGE, VERSION, engine, job.format, prompts, brand.palette, brand.name)
    if is_cached(job, STAGE, key, outs, ctx):
        job.image_paths = outs
        log(job, "images: cached, skipped")
        return job

    for stale in img_dir.glob("beat_*.png"):
        if stale not in outs:
            stale.unlink()
    for i, (prompt, out) in enumerate(zip(prompts, outs, strict=True)):
        if engine == "mock":
            _mock_image(out, job.script.beats[i].visual_prompt, i, len(prompts), brand, job.format)
        else:
            seed = int(sha256_bytes(f"{job.job_id}:{i}".encode())[:8], 16)
            _mflux_image(out, prompt, seed, engine, job.format)
    job.image_paths = outs
    model = "placeholder" if engine == "mock" else MFLUX_MODELS[engine][0]
    write_cache(job, STAGE, key, outs, model=model, prompts=prompts)
    log(job, f"images: {engine} ({model}) {len(outs)} stills")
    return job
