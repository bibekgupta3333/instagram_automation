"""Stage 5: one visual per beat (a still, or a stock clip that render trims and mutes).

Engines (STUDIO_IMAGES_ENGINE or --engine images=NAME; default ``auto``):
- ``auto``: per beat, tries STUDIO_IMAGES_AUTO_ORDER (default z-image-turbo -> pexels ->
  bedrock-stability -> mock). Z-Image is skipped unless its 4-bit repo is downloaded; Pexels is
  used only with PEXELS_API_KEY and when the beat suits stock; mock is a loud last resort.
- ``bedrock-stability``: Stability AI on Bedrock (Core $0.04/image by default; 9:16 shorts,
  16:9 long-form; brand style appended; seed per job + beat). Capped per video and logged to
  the spend ledger.
- ``pexels``: free stock clips/photos, attribution in images/attribution.txt.
- ``z-image-turbo`` / ``flux2-klein-4b``: local mflux, Apache-2.0, pre-quantized 4-bit repos
  (doc 18 §1). Z-Image-Turbo q4 (5.9 GB) is the default (Configuration A). The raw still
  (576x1024 / 1024x576) goes to images/raw/, then Real-ESRGAN (visuals/upscale.py) makes the
  frame-size beat image; without it the raw still is used and render scales it.
- ``bedrock-luma``: Luma Ray2 hero clip for beat 0 (gated stub; see visuals/luma.py), the
  rest as ``auto``.
- ``mock``: a 1080x1920 (or 1920x1080) ffmpeg placeholder (brand colour + wrapped prompt).

Per-beat results live in images/visuals.json, so a resumed run never re-pays for a beat it
already has.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import warnings
from pathlib import Path
from typing import Any

from studio.config import VisualsConfig
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
from studio.stages.visuals import BeatSpec, Visual, is_video
from studio.stages.visuals import luma as luma_src
from studio.stages.visuals import pexels as pexels_src
from studio.stages.visuals import stability as stability_src
from studio.stages.visuals.common import logger, record_cost

STAGE = "images"
VERSION = 5  # 5: Z-Image q4 default, 576x1024 raw + Real-ESRGAN to frame size (doc 18)

# engine id -> (model name, default mflux CLI). Override the command with
# STUDIO_MFLUX_CMD_<ENGINE> (e.g. STUDIO_MFLUX_CMD_Z_IMAGE_TURBO).
# Always point mflux at PRE-QUANTIZED repos. `-q N` on a base model id makes mflux download
# the full-precision weights first (Z-Image 32.9 GB, klein 4B ~16 GB) - see doc 18 §3.
# Verified with mflux 0.23 (2026-10-16): `mflux-generate-z-image-turbo --model <repo>` loads the
# q4 repo and infers z-image-turbo from the name (no --base-model needed); 9 steps is the CLI
# default; --guidance is ignored (guidance-distilled); width/height must be multiples of 16.
MFLUX_MODELS: dict[str, tuple[str, list[str]]] = {
    "flux2-klein-4b": (  # smallest good model: 4.6 GB, Apache-2.0
        "FLUX.2 [klein] 4B (mflux 4-bit)",
        ["mflux-generate-flux2", "--model", "mflux-community/flux2-klein-4b-mflux-q4",
         "--base-model", "flux2-klein-4b", "--steps", "4", "--guidance", "1.0"],
    ),
    "z-image-turbo": (  # best quality <= 6 GB: 5.9 GB, Apache-2.0
        "Z-Image-Turbo (mflux 4-bit)",
        ["mflux-generate-z-image-turbo", "--model", "mflux-community/z-image-turbo-mflux-q4",
         "--steps", "9"],
    ),
}
UPSCALED_SUFFIX = " + Real-ESRGAN x4"

SOURCE_ENGINES = ("pexels", "bedrock-stability")
ENGINES = ("auto", "mock", *SOURCE_ENGINES, "bedrock-luma", *MFLUX_MODELS)
MEDIA_SUFFIXES = (".png", ".jpg", ".jpeg", ".mp4", ".mov", ".m4v", ".webm")

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def frame_size(fmt: str) -> tuple[int, int]:
    return (1080, 1920) if fmt == "short" else (1920, 1080)


def gen_size(fmt: str) -> tuple[int, int]:
    """mflux needs multiples of 16. 576x1024 is ~65 s of denoising on an M4 Pro vs ~95-105 s
    for 720x1280 (output/benchmarks/zimage); Real-ESRGAN x4 then covers 1080x1920."""
    return (576, 1024) if fmt == "short" else (1024, 576)


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
            f"Images engine {engine!r} needs mflux, which is not installed. Run `make deps` "
            "(uv sync --extra local-audio --extra local-image --extra bedrock --group dev) or "
            "use --mock / --engine images=mock."
        ) from exc
    override = os.environ.get("STUDIO_MFLUX_CMD_" + engine.upper().replace("-", "_"))
    cmd = shlex.split(override) if override else list(MFLUX_MODELS[engine][1])
    repo = _mflux_repo(cmd)
    if repo and not _hf_cached(repo) and os.environ.get("STUDIO_ALLOW_MODEL_DOWNLOAD") != "1":
        # Never let a render silently trigger a multi-GB download (disk is tight; doc 18 §3).
        raise EngineUnavailable(
            f"{engine}: model {repo} is not downloaded. Run `make models STAGE=image` "
            "(disk-checked) or set STUDIO_ALLOW_MODEL_DOWNLOAD=1"
        )
    exe = _resolve_cli(cmd[0])
    if exe is None:
        raise EngineUnavailable(f"{cmd[0]} not on PATH; set STUDIO_MFLUX_CMD_* to the mflux CLI")
    return [exe, *cmd[1:]]


def _resolve_cli(name: str) -> str | None:
    """The mflux CLI from this interpreter's venv (`.venv/bin`), else from PATH."""
    if os.sep in name:
        return name if Path(name).exists() else None
    local = Path(sys.executable).parent / name
    return str(local) if local.exists() else shutil.which(name)


def _mflux_repo(cmd: list[str]) -> str | None:
    if "--model" in cmd:
        value = cmd[cmd.index("--model") + 1]
        return value if "/" in value else None
    return None


def _hf_cached(repo: str) -> bool:
    """True if the HF cache already holds weights for `repo` (no network call)."""
    hub = Path(os.environ.get("HF_HUB_CACHE")
               or Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")
    snaps = hub / f"models--{repo.replace('/', '--')}" / "snapshots"
    return snaps.is_dir() and any(p.suffix == ".safetensors" for p in snaps.rglob("*"))


def _mflux_image(out: Path, prompt: str, seed: int, engine: str, fmt: str) -> None:
    w, h = gen_size(fmt)
    cmd = [*_mflux_cmd(engine), "--prompt", prompt, "--width", str(w), "--height", str(h),
           "--seed", str(seed), "--output", str(out)]
    env = dict(os.environ)
    if env.get("STUDIO_ALLOW_MODEL_DOWNLOAD") != "1":
        env["HF_HUB_OFFLINE"] = "1"  # belt and braces: mflux may not fetch anything (doc 18 §3)
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)
    if proc.returncode != 0 or not out.exists():
        raise StageError(f"mflux failed for {out.name}: {proc.stderr.strip()[-800:]}")


def _to_frame(raw: Path, out: Path, fmt: str) -> bool:
    """Upscale the raw still to the frame size with Real-ESRGAN when available (True), else
    copy it unchanged and let render scale it (False)."""
    try:
        from studio.stages.visuals.upscale import upscale_to
    except ImportError:
        upscale_to = None
    if upscale_to is not None:
        try:
            upscale_to(raw, out, *frame_size(fmt))
            return True
        except (StageError, ImportError, ValueError, RuntimeError, OSError) as exc:
            logger.warning("images: upscale skipped for %s: %s", raw.name, exc)
    shutil.copyfile(raw, out)
    return False


def unload() -> None:
    """mflux runs as a subprocess (freed when each call exits); free the upscaler if loaded."""
    mod = sys.modules.get("studio.stages.visuals.upscale")
    if mod is not None and hasattr(mod, "unload"):
        mod.unload()


class ImageBudgetExceeded(StageError):
    """Paid stills for this video would go over STUDIO_IMAGES_MAX_USD_PER_VIDEO."""


class _Run:
    """Per-run state: config, spend so far, Pexels ids already used (no repeated clips)."""

    def __init__(self, job: VideoJob, brand: BrandPack, cfg: VisualsConfig, total: int):
        self.job, self.brand, self.cfg, self.total = job, brand, cfg, total
        self.spent = 0.0
        self.used: set[str] = set()


def _produce(engine: str, spec: BeatSpec, st: _Run) -> Visual:
    """One beat from one named engine. Raises StageError / NotImplementedError on failure."""
    if engine == "mock":
        out = spec.out_stem.with_suffix(".png")
        _mock_image(out, spec.visual_prompt, spec.index, st.total, st.brand, spec.fmt)
        return Visual(path=out, kind="image", engine="mock", model="placeholder")
    if engine in MFLUX_MODELS:
        out = spec.out_stem.with_suffix(".png")
        raw = out.parent / "raw" / out.name
        seed = int(sha256_bytes(f"{spec.job_id}:{spec.index}".encode())[:8], 16)
        _mflux_image(raw, spec.prompt, seed, engine, spec.fmt)
        upscaled = _to_frame(raw, out, spec.fmt)
        model = MFLUX_MODELS[engine][0] + (UPSCALED_SUFFIX if upscaled else "")
        return Visual(path=out, kind="image", engine=engine, model=model)
    if engine == "pexels":
        return pexels_src.fetch(spec, api_key=st.cfg.pexels_api_key, used=st.used)
    if engine == "bedrock-stability":
        model_id, price = stability_src.resolve_model(st.cfg.stability_model)
        if st.spent + price > st.cfg.max_image_usd_per_video + 1e-9:
            raise ImageBudgetExceeded(
                f"images: ${st.spent + price:.2f} would exceed STUDIO_IMAGES_MAX_USD_PER_VIDEO="
                f"${st.cfg.max_image_usd_per_video:.2f}"
            )
        vis = stability_src.generate(spec, model=st.cfg.stability_model,
                                     region=st.cfg.bedrock_image_region,
                                     negative_prompt=st.cfg.negative_prompt)
        record_cost(channel=st.job.channel, job_id=st.job.job_id, step="images",
                    model=model_id, usd=vis.usd)
        return vis
    if engine == "bedrock-luma":
        return luma_src.generate(spec, st.cfg)
    raise StageError(f"images: unknown engine {engine!r} ({', '.join(ENGINES)})")


def _auto(spec: BeatSpec, st: _Run, order: tuple[str, ...]) -> Visual:
    reasons: list[str] = []
    for engine in order:
        if engine == "pexels":
            if not st.cfg.pexels_api_key:
                reasons.append("pexels: no PEXELS_API_KEY")
                continue
            if not pexels_src.suits_stock(spec, st.brand, st.cfg.pexels_channels):
                reasons.append("pexels: beat/brand style doesn't suit stock")
                continue
        if engine == "mock":
            msg = (f"images: WARNING beat {spec.index:02d} fell back to a MOCK PLACEHOLDER "
                   f"({'; '.join(reasons) or 'no engines'}). Do not publish this render.")
            warnings.warn(msg, stacklevel=2)
            logger.warning(msg)
            log(st.job, msg)
        try:
            return _produce(engine, spec, st)
        except (StageError, NotImplementedError) as exc:
            reasons.append(f"{engine}: {exc}")
            log(st.job, f"images: beat {spec.index:02d} {engine} unavailable: "
                        f"{str(exc).splitlines()[0][:200]}")
    raise StageError(f"images: no engine produced beat {spec.index:02d}: {'; '.join(reasons)}")


def _beat_visual(engine: str, spec: BeatSpec, st: _Run) -> Visual:
    if engine == "auto":
        return _auto(spec, st, st.cfg.auto_order)
    if engine == "bedrock-luma":
        if spec.index == 0:  # hero shot only; the rest of the video is `auto`
            try:
                return _produce("bedrock-luma", spec, st)
            except (StageError, NotImplementedError) as exc:
                log(st.job, f"images: bedrock-luma hero skipped: {str(exc).splitlines()[0]}")
        return _auto(spec, st, st.cfg.auto_order)
    return _produce(engine, spec, st)


def _manifest_path(img_dir: Path) -> Path:
    return img_dir / "visuals.json"


def _load_manifest(img_dir: Path) -> dict[str, Any]:
    try:
        return json.loads(_manifest_path(img_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_manifest(img_dir: Path, key: str, entries: list[dict[str, Any]]) -> None:
    path = _manifest_path(img_dir)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps({"key": key, "beats": entries}, indent=1))
    tmp.replace(path)


def _write_attribution(img_dir: Path, visuals: list[Visual]) -> None:
    lines = [v.source["credit"] for v in visuals if v.engine == "pexels" and v.source.get("credit")]
    path = img_dir / "attribution.txt"
    if lines:
        path.write_text("\n".join(dict.fromkeys(lines)) + "\n")
    else:
        path.unlink(missing_ok=True)


def run(job: VideoJob, brand: BrandPack, ctx: StageContext) -> VideoJob:
    if job.script is None:
        raise StageError("images: no script")
    engine = ctx.engine("images")
    if engine not in ENGINES:
        raise StageError(f"images: unknown engine {engine!r} ({', '.join(ENGINES)})")
    cfg = VisualsConfig.from_env()
    img_dir = Path(job.workdir) / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    beats = job.script.beats
    prompts = [image_prompt(b.visual_prompt, brand) for b in beats]
    config_part = {"order": list(cfg.auto_order), "stability": cfg.stability_model,
                   "neg": cfg.negative_prompt} if engine != "mock" else {}
    key = hash_inputs(STAGE, VERSION, engine, config_part, job.format, prompts, brand.palette,
                      brand.name)
    beat_keys = [hash_inputs(STAGE, VERSION, engine, config_part, job.format, p,
                             b.visual_prompt, brand.palette, brand.name, i)
                 for i, (p, b) in enumerate(zip(prompts, beats, strict=True))]

    manifest = _load_manifest(img_dir)
    prior = {e.get("beat_key"): e for e in manifest.get("beats", []) if isinstance(e, dict)}
    prior_vis = [Visual.from_json(e["visual"]) for e in manifest.get("beats", [])
                 if isinstance(e, dict) and "visual" in e]
    fell_back = any(v.engine == "mock" for v in prior_vis) and engine != "mock"
    if (manifest.get("key") == key and len(prior_vis) == len(beats) and not fell_back
            and is_cached(job, STAGE, key, [v.path for v in prior_vis], ctx)):
        job.image_paths = [v.path for v in prior_vis]
        log(job, "images: cached, skipped")
        return job

    st = _Run(job, brand, cfg, len(beats))
    visuals: list[Visual] = []
    entries: list[dict[str, Any]] = []
    for i, (beat, prompt) in enumerate(zip(beats, prompts, strict=True)):
        spec = BeatSpec(job_id=job.job_id, channel=job.channel, index=i, fmt=job.format,
                        visual_prompt=beat.visual_prompt, prompt=prompt,
                        out_stem=img_dir / f"beat_{i:02d}", style_prompt=brand.style_prompt)
        old = prior.get(beat_keys[i])
        vis: Visual | None = None
        if old and not ctx.force:
            cand = Visual.from_json(old["visual"])
            if cand.path.exists() and (cand.engine != "mock" or engine == "mock"):
                vis = cand  # already paid for / downloaded: reuse
        if vis is None:
            for stale in img_dir.glob(f"beat_{i:02d}.*"):
                if stale.suffix.lower() in MEDIA_SUFFIXES or stale.name.endswith(".part"):
                    stale.unlink()
            vis = _beat_visual(engine, spec, st)
        st.spent += vis.usd
        if vis.source.get("id") is not None and vis.engine == "pexels":
            st.used.add(f"{vis.kind if vis.kind == 'video' else 'photo'}:{vis.source['id']}")
        visuals.append(vis)
        entries.append({"beat": i, "beat_key": beat_keys[i], "visual": vis.to_json()})
        _write_manifest(img_dir, "", entries)  # partial: progress survives a crash

    outs = [v.path for v in visuals]
    for stale in img_dir.glob("beat_*"):
        if stale not in outs and (stale.suffix.lower() in MEDIA_SUFFIXES
                                  or stale.name.endswith(".part")):
            stale.unlink()
    _write_manifest(img_dir, key, entries)
    _write_attribution(img_dir, visuals)
    job.image_paths = outs
    counts: dict[str, int] = {}
    for v in visuals:
        counts[f"{v.engine}/{v.kind}"] = counts.get(f"{v.engine}/{v.kind}", 0) + 1
    models = sorted({v.model for v in visuals})
    write_cache(job, STAGE, key, outs, model=",".join(models), prompts=prompts,
                usd=round(st.spent, 4), engines=counts)
    summary = ", ".join(f"{k}={n}" for k, n in sorted(counts.items()))
    log(job, f"images: {engine} {len(outs)} visuals ({summary}) ${st.spent:.2f}")
    clips = sum(is_video(p) for p in outs)
    if clips:
        log(job, f"images: {clips} stock clips; credits in images/attribution.txt")
    return job
