"""Stage 5: one visual per beat (a still, or a stock clip that render trims and mutes).

Engines (STUDIO_IMAGES_ENGINE or --engine images=NAME; default ``auto``):
- ``auto``: per beat, tries the brand's visual order (``VisualsConfig.visual_order``): by
  default z-image-turbo -> pixabay -> mock, but stock-first channels (car-cost-truth,
  square-foot-money, business-model-files) try Pixabay *video* first for beats that carry a
  ``stock_query`` and whose shot isn't a diagram/cutaway: real footage beats an AI still for
  real-world subjects. STUDIO_IMAGES_AUTO_ORDER overrides the order for every beat. Z-Image is
  skipped unless its 4-bit repo is downloaded; Pixabay is used only with PIXABAY_API_KEY and
  when the beat suits stock; mock is a loud last resort.
- ``z-image-turbo`` / ``flux2-klein-4b``: local mflux, Apache-2.0, pre-quantized 4-bit repos
  (doc 18 §1). Z-Image-Turbo q4 (5.9 GB) is the default (Configuration A). Prompts come from
  visuals/prompts.py (beat visual + shot framing + one brand style per video, explicit no-text
  clause; Z-Image has no negative prompt). Seeds are a per-video base seed + the beat index.
  The raw still (gen_size) goes to images/raw/, then Real-ESRGAN (visuals/upscale.py) makes the
  frame-size beat image; without it the raw still is used and render scales it.
- ``pixabay``: free stock clips/photos (Pixabay Content License), searches cached 24 h in
  output/.cache/pixabay/, credits in images/attribution.txt.
- ``mock``: a 1080x1920 (or 1920x1080) ffmpeg placeholder (brand colour + wrapped prompt).

Motion comes from local parallax in render. Bedrock is for TEXT models only: the
bedrock-stability / bedrock-luma engines were removed 2026-10-16 and naming them is an error.

Per-beat results live in images/visuals.json, so a resumed run never re-fetches a beat it
already has. Each entry also records the beat's ``shot``; motion.py reads it to pick a camera
move that suits the shot (wide -> dolly/pan, close-up -> push-in, aerial -> rise/orbit,
diagram -> gentle zoom).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import warnings
from pathlib import Path
from typing import Any

from studio.config import VisualsConfig, check_image_engine
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
    write_cache,
)
from studio.stages.visuals import BeatSpec, Visual, is_video
from studio.stages.visuals import pixabay as pixabay_src
from studio.stages.visuals.common import beat_seed, logger
from studio.stages.visuals.prompts import build_prompt, shot_class

STAGE = "images"
VERSION = 10  # 10: anchored Pixabay searches and tag relevance; reject technical stock shots
# 9: bind caches to job seed, shot metadata and stock eligibility
# 8: prompt builder (shot framing, no-text clause), per-video base seed, per-brand
# visual order (stock-first), Z-Image steps/size from VisualsConfig;
# 7: 7: Bedrock Stability/Luma removed (Bedrock is text-only); 6: Pixabay replaces
# Pexels as the stock source; 5: Z-Image q4 + Real-ESRGAN

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
         "--steps", "9"],  # replaced by VisualsConfig.zimage_steps at run time
    ),
}
UPSCALED_SUFFIX = " + Real-ESRGAN x4"

ENGINES = ("auto", "mock", "pixabay", *MFLUX_MODELS)
MEDIA_SUFFIXES = (".png", ".jpg", ".jpeg", ".mp4", ".mov", ".m4v", ".webm")

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def frame_size(fmt: str) -> tuple[int, int]:
    return (1080, 1920) if fmt == "short" else (1920, 1080)


def gen_size(fmt: str, size: tuple[int, int] | None = None) -> tuple[int, int]:
    """mflux needs multiples of 16. `size` is the portrait (short) size, default
    VisualsConfig.zimage_size (640x1152: the best sharpness per second after Real-ESRGAN,
    see VisualsConfig); long-form uses the transpose. Real-ESRGAN x4 then covers the frame."""
    w, h = size or VisualsConfig().zimage_size
    return (w, h) if fmt == "short" else (h, w)


def image_prompt(visual_prompt: str, brand: BrandPack, shot: str = "", fmt: str = "short") -> str:
    """The generation prompt for one beat (visuals/prompts.py)."""
    return build_prompt(visual_prompt, brand, shot, fmt)


# shots stock footage can't stand in for, whatever the channel
_NO_STOCK_SHOTS = ("diagram", "cutaway")
_NO_STOCK_VISUAL = re.compile(
    r"\b(diagram|infographic|chart|graph|cutaway|cross[- ]section|schematic|exploded view)\b",
    re.IGNORECASE)


def stock_beat(spec: BeatSpec) -> bool:
    """True when a stock-first channel should try Pixabay first for this beat: the script gave
    a stock_query and the shot/visual isn't a diagram or cutaway."""
    return bool(spec.stock_query.strip()) and shot_class(spec.shot) not in _NO_STOCK_SHOTS \
        and not _NO_STOCK_VISUAL.search(f"{spec.shot} {spec.visual_prompt}")


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


def _mflux_cmd(engine: str, steps: int | None = None) -> list[str]:
    # Presence checks must not import MLX or initialise its GPU runtime in this process.
    if "mflux" not in sys.modules and importlib.util.find_spec("mflux") is None:
        raise EngineUnavailable(
            f"Images engine {engine!r} needs mflux, which is not installed. Run `make deps` "
            "(uv sync --extra local-audio --extra local-image --extra bedrock --group dev) or "
            "use --mock / --engine images=mock."
        )
    override = os.environ.get("STUDIO_MFLUX_CMD_" + engine.upper().replace("-", "_"))
    cmd = shlex.split(override) if override else list(MFLUX_MODELS[engine][1])
    if steps is not None and not override and engine == "z-image-turbo" and "--steps" in cmd:
        cmd[cmd.index("--steps") + 1] = str(steps)
    repo = _mflux_repo(cmd)
    expected = _mflux_repo(MFLUX_MODELS[engine][1])
    if repo != expected or cmd.count("--model") != 1 or any(
        arg.startswith(("--model=", "-q", "--quantiz")) for arg in cmd[1:]
    ):
        raise EngineUnavailable(f"{engine}: use only the installed {expected} repo without -q")
    if not _hf_cached(repo):
        # Never let a render silently trigger a multi-GB download (disk is tight; doc 18 §3).
        raise EngineUnavailable(
            f"{engine}: model {repo} is not downloaded. Run `make models STAGE=image` "
            "after `make models-dry STAGE=image` (disk-checked)"
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
    if "--model" in cmd and cmd.index("--model") + 1 < len(cmd):
        value = cmd[cmd.index("--model") + 1]
        return value if "/" in value else None
    return None


def _hf_cached(repo: str) -> bool:
    """True if the HF cache already holds weights for `repo` (no network call)."""
    hub = Path(os.environ.get("HF_HUB_CACHE")
               or Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")
    snaps = hub / f"models--{repo.replace('/', '--')}" / "snapshots"
    return snaps.is_dir() and any(p.suffix == ".safetensors" for p in snaps.rglob("*"))


def _mflux_image(out: Path, prompt: str, seed: int, engine: str, fmt: str,
                 cfg: VisualsConfig | None = None) -> None:
    cfg = cfg or VisualsConfig()
    w, h = gen_size(fmt, cfg.zimage_size)
    cmd = [*_mflux_cmd(engine, cfg.zimage_steps), "--prompt", prompt, "--width", str(w), "--height", str(h),
           "--seed", str(seed), "--output", str(out)]
    env = dict(os.environ)
    env["HF_HUB_OFFLINE"] = "1"  # render must never download, even with a legacy override
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


class _Run:
    """Per-run state: config, stock ids already used (no repeated clips)."""

    def __init__(self, job: VideoJob, brand: BrandPack, cfg: VisualsConfig, total: int,
                 cache_dir: Path | None = None):
        self.job, self.brand, self.cfg, self.total = job, brand, cfg, total
        self.cache_dir = cache_dir
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
        unload()  # ESRGAN from a previous beat must not overlap the mflux subprocess
        _mflux_image(raw, spec.prompt, beat_seed(spec.job_id, spec.index), engine, spec.fmt,
                     st.cfg if st is not None else None)
        try:
            upscaled = _to_frame(raw, out, spec.fmt)
        finally:
            unload()  # also release the upscaler on failed beats / fallback
        model = MFLUX_MODELS[engine][0] + (UPSCALED_SUFFIX if upscaled else "")
        return Visual(path=out, kind="image", engine=engine, model=model)
    if engine == "pixabay":
        return pixabay_src.fetch(spec, api_key=st.cfg.pixabay_api_key, used=st.used,
                                 cache_dir=st.cache_dir)
    raise StageError(f"images: unknown engine {engine!r} ({', '.join(ENGINES)})")


def _auto(spec: BeatSpec, st: _Run, order: tuple[str, ...]) -> Visual:
    reasons: list[str] = []
    for engine in order:
        if engine == "pixabay":
            if not st.cfg.pixabay_api_key:
                reasons.append("pixabay: no PIXABAY_API_KEY")
                continue
            allow = st.cfg.pixabay_channels
            if st.cfg.is_stock_first(st.brand.slug) and stock_beat(spec):
                allow = (*allow, st.brand.slug)  # the brand style is a look, not the subject
            if not pixabay_src.suits_stock(spec, st.brand, allow):
                reasons.append("pixabay: beat/brand style doesn't suit stock")
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


def beat_order(spec: BeatSpec, cfg: VisualsConfig) -> tuple[str, ...]:
    """The `auto` engine order for one beat (per brand; see VisualsConfig.visual_order)."""
    return cfg.visual_order(spec.channel, stock_beat(spec))


def _beat_visual(engine: str, spec: BeatSpec, st: _Run) -> Visual:
    if engine == "auto":
        return _auto(spec, st, beat_order(spec, st.cfg))
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
    lines = [v.source["credit"] for v in visuals if v.engine == "pixabay" and v.source.get("credit")]
    path = img_dir / "attribution.txt"
    if lines:
        path.write_text("\n".join(dict.fromkeys(lines)) + "\n")
    else:
        path.unlink(missing_ok=True)


def run(job: VideoJob, brand: BrandPack, ctx: StageContext) -> VideoJob:
    if job.script is None:
        raise StageError("images: no script")
    engine = ctx.engine("images")
    try:
        check_image_engine(engine, "--engine images")
        cfg = VisualsConfig.from_env()
    except ValueError as exc:
        raise StageError(str(exc)) from exc
    if engine not in ENGINES:
        raise StageError(f"images: unknown engine {engine!r} ({', '.join(ENGINES)})")
    img_dir = Path(job.workdir) / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    beats = job.script.beats
    prompts = [image_prompt(b.visual_prompt, brand, b.shot, job.format) for b in beats]
    specs = [BeatSpec(job_id=job.job_id, channel=job.channel, index=i, fmt=job.format,
                      visual_prompt=b.visual_prompt, prompt=p, out_stem=img_dir / f"beat_{i:02d}",
                      style_prompt=brand.style_prompt, shot=b.shot,
                      stock_query=pixabay_src.anchor_query(b.stock_query, job.channel))
             for i, (b, p) in enumerate(zip(beats, prompts, strict=True))]
    gen = {"steps": cfg.zimage_steps, "size": list(cfg.zimage_size)}
    orders = [list(beat_order(s, cfg)) if engine == "auto" else [] for s in specs]
    config_part = {"orders": orders, "stock_channels": cfg.pixabay_channels,
                   "stock_first_channels": cfg.stock_first_channels, **gen} if engine != "mock" else {}
    key = hash_inputs(STAGE, VERSION, engine, config_part, job.job_id, job.format,
                      prompts, brand.palette, brand.name, [b.stock_query for b in beats],
                      [b.shot for b in beats])
    beat_keys = [hash_inputs(STAGE, VERSION, engine, gen if engine != "mock" else {},
                             orders[i], cfg.pixabay_channels, cfg.stock_first_channels,
                             job.job_id, job.format, p, b.visual_prompt, b.stock_query, b.shot,
                             brand.palette, brand.name, i)
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

    st = _Run(job, brand, cfg, len(beats),
              cache_dir=Path(ctx.settings.output_dir) / ".cache" / "pixabay")
    reusable: dict[int, Visual] = {}
    if not ctx.force:
        for i, beat_key in enumerate(beat_keys):
            old = prior.get(beat_key)
            if not old:
                continue
            cand = Visual.from_json(old["visual"])
            if cand.path.exists() and (cand.engine != "mock" or engine == "mock"):
                reusable[i] = cand
                if cand.engine == "pixabay" and cand.source.get("id") is not None:
                    kind = "video" if cand.kind == "video" else "photo"
                    st.used.add(f"{kind}:{cand.source['id']}")
    visuals: list[Visual] = []
    entries: list[dict[str, Any]] = []
    for i, (beat, spec) in enumerate(zip(beats, specs, strict=True)):
        vis = reusable.get(i)
        if vis is None:
            for stale in img_dir.glob(f"beat_{i:02d}.*"):
                if stale.suffix.lower() in MEDIA_SUFFIXES or stale.name.endswith(".part"):
                    stale.unlink()
            vis = _beat_visual(engine, spec, st)
        if vis.source.get("id") is not None and vis.engine == "pixabay":
            st.used.add(f"{vis.kind if vis.kind == 'video' else 'photo'}:{vis.source['id']}")
        visuals.append(vis)
        entries.append({"beat": i, "beat_key": beat_keys[i], "shot": beat.shot,
                        "visual": vis.to_json()})
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
                engines=counts)
    summary = ", ".join(f"{k}={n}" for k, n in sorted(counts.items()))
    log(job, f"images: {engine} {len(outs)} visuals ({summary})")
    clips = sum(is_video(p) for p in outs)
    if clips:
        log(job, f"images: {clips} stock clips; credits in images/attribution.txt")
    return job
