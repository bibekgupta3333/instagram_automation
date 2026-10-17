"""2.5D depth parallax for still beats: our own licence-clean renderer (doc 18 §2, §6).

Depth Anything V2 **Small** (Apache-2.0; Base/Large are CC-BY-NC, never use them) runs with
transformers on MPS and gives a relative depth map per still, cached per image hash. The
camera move is an OpenCV `remap` whose per-pixel offsets are weighted by depth, so near things
move more than far ones. No DepthFlow (AGPL) code is used.

Edge handling: the depth map is grey-dilated (near regions grow a few pixels into the
background) and then blurred, so foreground objects move as a rigid piece and the transition
lands in the background; the background revealed behind the foreground comes from a clean
plate (foreground masked out and filled with cv2.inpaint at low resolution), composited under
the foreground with a soft alpha. With the subtle default amplitude (3% of the short side) this
removes the rubber-band stretching that a plain depth warp shows at silhouettes.

Camera moves follow the beat's shot type when the images stage recorded one (``shot`` in
images/visuals.json): wide -> slow dolly or pan, close-up -> push-in, aerial -> rise or orbit,
cutaway -> slow dolly or orbit, diagram -> gentle zoom only (no depth warp: flat graphics have
unreliable depth). Within that family the job's rotation still varies the move. The amplitude
adapts to depth confidence (``depth_confidence``): up to +45% for a clean, smooth, well-separated
depth map, down to -10% for a noisy/flat one. Moves ease in and out (cosine, with a linear share
so a 3.5 s shot never looks stop-start).

Frames are piped as raw BGR to ffmpeg (h264_videotoolbox, libx264 fallback); the clip is
video-only. The model is never downloaded here: `scripts/setup/20-pull-models.sh --stage video`.
"""

from __future__ import annotations

import gc
import hashlib
import importlib.util
import math
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

from studio.stages.base import EngineUnavailable, StageError, require_binary
from studio.stages.visuals.common import configure_cpu_threads
from studio.stages.visuals.upscale import cover_geometry, hf_hub_dir

REPO = "depth-anything/Depth-Anything-V2-Small-hf"
MOTION_VERSION = 3  # 3: diagram zooms skip depth inference; bounded CPU threads
# 2: ease-in-out, shot-driven presets (push-in, gentle-zoom), depth-confidence amplitude
FPS = 30
PRESETS: tuple[str, ...] = ("dolly-in", "dolly-out", "pan-left", "pan-right", "orbit", "rise")
SHOT_ONLY_PRESETS: tuple[str, ...] = ("push-in", "gentle-zoom")  # chosen by shot type only
ALL_PRESETS: tuple[str, ...] = (*PRESETS, *SHOT_ONLY_PRESETS)
DEFAULT_AMPLITUDE = 0.03  # parallax differential, fraction of the frame's short side
AMP_GAIN = (0.9, 1.45)  # amplitude multiplier at depth confidence 0 and 1
# shot class (studio.stages.visuals.prompts.shot_class) -> presets that suit it
SHOT_PRESETS: dict[str, tuple[str, ...]] = {
    "wide": ("dolly-in", "pan-right", "pan-left", "dolly-out"),
    "close": ("push-in", "dolly-in"),
    "aerial": ("rise", "orbit"),
    "top": ("dolly-in", "rise"),
    "low": ("rise", "dolly-in"),
    "cutaway": ("dolly-in", "orbit", "dolly-out"),
    "diagram": ("gentle-zoom",),
}
DEPTH_RES = 518  # Depth Anything's native short side (multiple of 14)

_model: Any = None
_processor: Any = None
_device: str | None = None


# ---------------------------------------------------------------- model location


def model_dir() -> Path:
    """Snapshot dir of the depth model in the local HF cache, or EngineUnavailable."""
    override = os.environ.get("STUDIO_DEPTH_MODEL_DIR")
    if override:
        p = Path(override)
        if (p / "config.json").exists() and (p / "model.safetensors").exists():
            return p
    snaps = hf_hub_dir() / f"models--{REPO.replace('/', '--')}" / "snapshots"
    if snaps.is_dir():
        for snap in sorted(snaps.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if all((snap / f).exists() for f in
                   ("config.json", "preprocessor_config.json", "model.safetensors")):
                return snap
    raise EngineUnavailable(
        f"motion: depth model {REPO} (~100 MB, Apache-2.0) is not downloaded. Run "
        "`scripts/setup/20-pull-models.sh --stage video` (disk-checked), or set "
        "STUDIO_DEPTH_MODEL_DIR"
    )


def available() -> bool:
    try:
        model_dir()
    except EngineUnavailable:
        return False
    return all(name in sys.modules or importlib.util.find_spec(name) is not None
               for name in ("cv2", "torch", "transformers"))


# ---------------------------------------------------------------- presets


def preset_sequence(seed: str, n: int) -> list[str]:
    """Shuffled per job, cycled: deterministic, and adjacent shots never repeat a preset."""
    order = list(PRESETS)
    rng = random.Random(f"parallax:{seed}")
    rng.shuffle(order)
    start = rng.randrange(len(order))
    return [order[(start + i) % len(order)] for i in range(n)]


def preset_for(job_id: str, index: int) -> str:
    """The preset for shot/beat `index` of job `job_id` (same as preset_sequence)."""
    return preset_sequence(job_id, index + 1)[index]


def preset_for_shot(shot: str, requested: str) -> str:
    """The preset for a still whose beat has shot type `shot`: `requested` (the job's
    rotation) when it suits the shot, else the family member it maps to, so consecutive shots
    of one job still vary. Unknown/empty shot -> `requested`."""
    from studio.stages.visuals.prompts import shot_class

    family = SHOT_PRESETS.get(shot_class(shot))
    if not family or requested in family:
        return requested
    idx = ALL_PRESETS.index(requested) if requested in ALL_PRESETS else 0
    return family[idx % len(family)]


def shot_hint(image: Path) -> str:
    """The beat's shot type recorded by the images stage in <image dir>/visuals.json."""
    import json

    try:
        data = json.loads((Path(image).parent / "visuals.json").read_text())
    except (OSError, ValueError):
        return ""
    for entry in data.get("beats", []) if isinstance(data, dict) else []:
        try:
            if Path(entry["visual"]["path"]).name == Path(image).name:
                return str(entry.get("shot") or "")
        except (KeyError, TypeError):
            continue
    return ""


def frame_size(fmt: str) -> tuple[int, int]:
    return (1080, 1920) if fmt == "short" else (1920, 1080)


def _ease(t: float) -> float:
    """Ease-in-out: a cosine ease with a 30% linear share, so the camera starts and settles
    gently but never stops dead inside a 3.5 s shot."""
    return 0.3 * t + 0.7 * (0.5 - 0.5 * math.cos(math.pi * t))


def camera(preset: str, t: float, amp_px: float) -> dict[str, float]:
    """Camera state at t in [0,1]: per-depth shift (px, near=1 far=0) and zoom.

    Offsets are *sampling* offsets: output(x) = source(c + (x - c) / zoom + shift), with
    shift = (gx + px * d, gy + py * d) and zoom = 1 + z0 + z1 * d for depth d.
    """
    if preset not in ALL_PRESETS:
        raise ValueError(f"unknown parallax preset {preset!r}; one of {ALL_PRESETS}")
    e = _ease(t)
    c = e - 0.5  # -0.5 .. 0.5: the move is centred on the still's framing
    g = 0.35 * amp_px  # global drift: the whole frame moves a little too
    s = {"gx": 0.0, "gy": 0.0, "px": 0.0, "py": 0.0, "z0": 0.0, "z1": 0.0}
    zoom = amp_px / 1000.0  # ~3% of a 1080 short side -> ~0.03 extra zoom on near things
    if preset in ("dolly-in", "dolly-out"):
        k = e if preset == "dolly-in" else 1.0 - e
        s["z0"], s["z1"] = 0.035 * k, zoom * k
    elif preset in ("pan-left", "pan-right"):
        sign = 1.0 if preset == "pan-right" else -1.0  # camera moves right: content goes left
        s["gx"], s["px"] = sign * c * g, sign * c * amp_px
        s["z0"] = 0.01
    elif preset == "orbit":
        # near and far move in opposite directions around a mid-depth pivot, on a slight arc
        s["px"] = c * amp_px * 1.2
        s["gx"] = -0.6 * c * amp_px
        s["py"] = -0.25 * amp_px * math.sin(math.pi * e)
        s["gy"] = 0.125 * amp_px * math.sin(math.pi * e)
        s["z0"] = 0.015
    elif preset == "rise":
        s["gy"], s["py"] = -c * g, -c * amp_px  # camera rises: content moves down
        s["z0"], s["z1"] = 0.01 + 0.01 * e, 0.0
    elif preset == "push-in":
        # close-up: a deliberate push towards the subject; near things grow faster, with a
        # slight upward drift so it doesn't read as a flat digital zoom
        s["z0"], s["z1"] = 0.06 * e, 1.6 * zoom * e
        s["gy"] = -0.15 * c * amp_px
    elif preset == "gentle-zoom":
        # diagrams: a plain slow zoom, no depth differential (flat art has no real depth)
        s["z0"] = 0.045 * e
    return s


def depth_confidence(d: Any) -> float:
    """0..1: how trustworthy a relative depth map is for parallax. High when the depth has
    distinct near/far layers (a share of strong depth edges), is smooth (variance is
    low-frequency structure, not texture noise) and is well spread; ~0 for flat graphics and
    placeholders (no depth edges) and for noisy maps. Calibrated on Z-Image stills
    (2026-10-17): real scenes 0.6-1.0, flat cards 0.0."""
    import cv2
    import numpy as np

    configure_cpu_threads(cv2=cv2)
    h, w = d.shape[:2]
    k = 128.0 / max(1, min(h, w))
    small = cv2.resize(d.astype(np.float32), (max(8, round(w * k)), max(8, round(h * k))),
                       interpolation=cv2.INTER_AREA)
    smooth = cv2.GaussianBlur(small, (0, 0), 3.0)
    structure = float(smooth.var())
    noise = float((small - smooth).var())
    smoothness = structure / max(structure + 4.0 * noise, 1e-9)
    lo, hi = np.percentile(smooth, [10, 90])
    gy, gx = np.gradient(small)
    edges = float((np.hypot(gx, gy) > 0.02).mean())
    c = (np.clip((edges - 0.01) / 0.04, 0, 1) * np.clip((smoothness - 0.8) / 0.15, 0, 1)
         * np.clip((float(hi - lo) - 0.3) / 0.3, 0, 1))
    return float(c)


def adaptive_amplitude(base: float, confidence: float) -> float:
    lo, hi = AMP_GAIN
    return base * (lo + (hi - lo) * float(min(max(confidence, 0.0), 1.0)))


# ---------------------------------------------------------------- depth


def _load() -> tuple[Any, Any, str]:
    global _model, _processor, _device
    if _model is not None:
        return _model, _processor, _device  # type: ignore[return-value]
    path = model_dir()
    try:
        import torch
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation
    except ImportError as exc:  # pragma: no cover
        raise EngineUnavailable(f"motion: needs torch + transformers ({exc})") from exc
    configure_cpu_threads(torch=torch)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    processor = AutoImageProcessor.from_pretrained(str(path), local_files_only=True)
    model = AutoModelForDepthEstimation.from_pretrained(str(path), local_files_only=True)
    model = model.to(device).eval()
    _model, _processor, _device = model, processor, device
    return _model, _processor, device


def _image_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:20]


def depth_map(image: Path, cache_dir: Path) -> Any:
    """Relative depth in [0,1] (1 = nearest) at the image's resolution, float32.
    Cached as float16 .npy per image content hash in `cache_dir`."""
    import cv2
    import numpy as np

    configure_cpu_threads(cv2=cv2)
    cache = Path(cache_dir) / f"{_image_hash(Path(image))}.v{MOTION_VERSION}.npy"
    bgr = cv2.imread(str(image), cv2.IMREAD_COLOR)
    if bgr is None:
        raise StageError(f"motion: cannot read {image}")
    h, w = bgr.shape[:2]
    if cache.exists():
        d = np.load(cache).astype(np.float32)
        if d.shape == (h, w):
            return d
    import torch
    from PIL import Image

    configure_cpu_threads(torch=torch)
    model, processor, device = _load()
    rgb = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    inputs = processor(images=rgb, return_tensors="pt")
    with torch.inference_mode():
        pred = model(pixel_values=inputs["pixel_values"].to(device)).predicted_depth
        pred = torch.nn.functional.interpolate(
            pred.unsqueeze(1), size=(h, w), mode="bicubic", align_corners=False
        )[0, 0].float().cpu().numpy()
    del inputs
    if device == "mps":
        torch.mps.empty_cache()
    lo, hi = np.percentile(pred, [1.0, 99.0])
    d = np.clip((pred - lo) / max(hi - lo, 1e-6), 0.0, 1.0).astype(np.float32)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp.npy")
    np.save(tmp, d.astype(np.float16))
    tmp.replace(cache)
    return d


def condition_depth(d: Any, short_side: int) -> Any:
    """Edge handling: grey-dilate near regions so silhouettes move rigidly, then blur so the
    offset field is smooth (no tearing) and the transition lands in the background."""
    import cv2

    configure_cpu_threads(cv2=cv2)
    r = max(3, round(short_side * 0.008)) | 1
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r, r))
    d = cv2.dilate(d, k)
    sigma = short_side * 0.006
    return cv2.GaussianBlur(d, (0, 0), sigmaX=sigma, sigmaY=sigma)


def clean_plate(bgr: Any, d: Any, short_side: int) -> tuple[Any, Any, Any]:
    """Split off the nearest layer using the *raw* depth (its edges sit on the silhouette).

    Returns (plate, alpha, grown): the background plate with the foreground removed and filled
    by cv2.inpaint (at half resolution, so it's fast), the foreground alpha (1 = foreground,
    ~1 px soft edge) and the grown foreground mask (bool). Pixels outside the foreground keep
    their original values, so only what the camera reveals comes from the fill. If the scene
    has no distinct near layer, alpha is all zeros (single-layer warp).
    """
    import cv2
    import numpy as np

    configure_cpu_threads(cv2=cv2)
    thr = float(np.clip(np.percentile(d, 50) + 0.25, 0.45, 0.85))
    fg = (d > thr).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))  # drop speckles
    zeros = np.zeros(d.shape, np.float32)
    if fg.mean() < 0.005 or fg.mean() > 0.6:
        return bgr, zeros, zeros.astype(bool)
    r = max(3, round(short_side * 0.012)) | 1
    grown = cv2.dilate(fg, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r, r)))
    # DA-V2's edges are a few px soft, so the alpha takes a thin ring of background with the
    # foreground (invisible) rather than leaving foreground pixels on the plate (dark bites)
    ra = max(3, round(short_side * 0.005)) | 1
    fg = cv2.dilate(fg, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ra, ra)))
    alpha = cv2.GaussianBlur(fg.astype(np.float32), (0, 0), 2.0)
    h, w = d.shape
    sw, sh = max(1, w // 2), max(1, h // 2)
    small = cv2.resize(bgr, (sw, sh), interpolation=cv2.INTER_AREA)
    mask = cv2.resize(grown * 255, (sw, sh), interpolation=cv2.INTER_NEAREST)
    filled = cv2.inpaint(small, mask, 3, cv2.INPAINT_TELEA)
    filled = cv2.resize(filled, (w, h), interpolation=cv2.INTER_CUBIC)
    core = cv2.dilate(fg, np.ones((5, 5), np.uint8)).astype(np.float32)
    m = cv2.GaussianBlur(core, (0, 0), 1.5)[..., None]
    plate = (bgr * (1 - m) + filled * m).astype(np.uint8)
    return plate, alpha, grown.astype(bool)


# ---------------------------------------------------------------- render


def _maps(cam: dict[str, float], d_small: Any, gx: Any, gy: Any, cx: float, cy: float,
          margin: int, scale: float, iters: int = 2) -> tuple[Any, Any]:
    """Backward sampling maps (low-res grid, base-canvas coords). Fixed-point iteration so the
    depth is read at the *source* location, which keeps silhouettes in place."""
    import cv2

    xo, yo = gx + margin, gy + margin  # output pixel -> base canvas coords
    d = cv2.remap(d_small, xo / scale, yo / scale, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    for _ in range(iters):
        z = 1.0 + cam["z0"] + cam["z1"] * d
        mx = cx + (xo - cx) / z + cam["gx"] + cam["px"] * d
        my = cy + (yo - cy) / z + cam["gy"] + cam["py"] * d
        d = cv2.remap(d_small, mx / scale, my / scale, cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_REFLECT)
    z = 1.0 + cam["z0"] + cam["z1"] * d
    mx = cx + (xo - cx) / z + cam["gx"] + cam["px"] * d
    my = cy + (yo - cy) / z + cam["gy"] + cam["py"] * d
    return mx, my


def _encoder_args(name: str) -> list[str]:
    if name == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", "14M", "-maxrate", "20M", "-bufsize", "28M",
                "-profile:v", "high", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-profile:v", "high"]


def _render(image: Path, out: Path, frames: int, fmt: str, preset: str, amp: float,
            cache_dir: Path, encoder: str, adaptive: bool = False) -> None:
    import cv2
    import numpy as np

    configure_cpu_threads(cv2=cv2)
    w, h = frame_size(fmt)
    short = min(w, h)
    src = cv2.imread(str(image), cv2.IMREAD_COLOR)
    if src is None:
        raise StageError(f"motion: cannot read {image}")
    # A flat diagram zoom has no depth differential and needs no depth model at all.
    d_full = (np.zeros(src.shape[:2], np.float32) if preset == "gentle-zoom"
              else depth_map(image, cache_dir))
    if adaptive and preset != "gentle-zoom":
        amp = adaptive_amplitude(amp, depth_confidence(d_full))
    amp_px = amp * short
    margin = math.ceil(0.7 * amp_px) + 8  # max |shift| is 0.675 amp_px (pan, rise)
    bw, bh = w + 2 * margin, h + 2 * margin
    sw, sh, x0, y0 = cover_geometry(src.shape[1], src.shape[0], bw, bh)
    interp = cv2.INTER_AREA if sw < src.shape[1] else cv2.INTER_LANCZOS4
    base = cv2.resize(src, (sw, sh), interpolation=interp)[y0:y0 + bh, x0:x0 + bw]
    raw = cv2.resize(d_full, (sw, sh), interpolation=cv2.INTER_LINEAR)[y0:y0 + bh, x0:x0 + bw]
    raw = np.ascontiguousarray(raw)
    del src, d_full
    dep = condition_depth(raw, short)
    if preset == "gentle-zoom":  # flat art: no depth warp, so nothing can tear or stretch
        dep = np.zeros_like(dep)
        alpha, grown = np.zeros_like(dep), np.zeros(dep.shape, bool)
        plate = base
    else:
        plate, alpha, grown = clean_plate(base, raw, short)
    layered = bool(alpha.max() > 0)

    scale = 2.0  # offset maps are smooth: compute them at half resolution, then upsample
    d_small = cv2.resize(dep, (int(bw / scale), int(bh / scale)), interpolation=cv2.INTER_AREA)
    gy, gx = np.mgrid[0:h // 2, 0:w // 2].astype(np.float32) * np.float32(scale)
    cx, cy = bw / 2.0, bh / 2.0
    # background layer: the plate moves with the depth *behind* the foreground
    d_bg_small = d_small
    if layered:
        # the plate moves with the depth *behind* the foreground: fill the foreground's
        # footprint with the nearest background level so the revealed strip doesn't stretch
        bg_level = float(np.percentile(raw[~grown], 90)) if (~grown).any() else 0.0
        d_bg = condition_depth(np.where(grown, np.minimum(raw, bg_level), raw).astype(np.float32),
                               short)
        d_bg_small = cv2.resize(d_bg, d_small.shape[::-1], interpolation=cv2.INTER_AREA)

    cmd = [require_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
           "-an", *_encoder_args(encoder), "-pix_fmt", "yuv420p", "-r", str(FPS),
           "-frames:v", str(frames), "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for i in range(frames):
            cam = camera(preset, i / max(frames - 1, 1), amp_px)
            mx, my = _maps(cam, d_small, gx, gy, cx, cy, margin, scale)
            mx = cv2.resize(mx, (w, h), interpolation=cv2.INTER_LINEAR)
            my = cv2.resize(my, (w, h), interpolation=cv2.INTER_LINEAR)
            frame = cv2.remap(base, mx, my, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
            if layered:
                bx, by = _maps(cam, d_bg_small, gx, gy, cx, cy, margin, scale)
                bx = cv2.resize(bx, (w, h), interpolation=cv2.INTER_LINEAR)
                by = cv2.resize(by, (w, h), interpolation=cv2.INTER_LINEAR)
                bg = cv2.remap(plate, bx, by, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
                a = cv2.remap(alpha, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
                a = a[..., None]
                frame = (frame * a + bg * (1.0 - a)).astype(np.uint8)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise StageError(f"motion: ffmpeg ({encoder}) failed: {err.strip()[-600:]}")


def parallax_clip(image: Path | str, out: Path | str, seconds: float, fmt: str,
                  preset: str | None = None, *, cache_dir: Path | str | None = None,
                  amplitude: float | None = None, shot: str | None = None) -> Path:
    """Render `seconds` (rounded to whole frames at 30 fps) of 2.5D parallax from a still.

    fmt "short" -> 1080x1920, "long" -> 1920x1080. preset None picks one deterministically
    from the image and output name; either way it is then fitted to the beat's shot type
    (`shot`, or the images stage's hint in visuals.json; see preset_for_shot). The amplitude
    adapts to depth confidence unless `amplitude` or STUDIO_PARALLAX_AMP fixes it. Depth is
    cached in `cache_dir` (default: <out dir>/.depth). Raises EngineUnavailable if the depth
    model isn't downloaded.
    """
    image, out = Path(image), Path(out)
    frames = max(1, round(seconds * FPS))
    if preset is None:
        preset = preset_for(f"{_image_hash(image)}:{out.name}", 0)
    if preset not in ALL_PRESETS:
        raise ValueError(f"unknown parallax preset {preset!r}; one of {ALL_PRESETS}")
    preset = preset_for_shot(shot_hint(image) if shot is None else shot, preset)
    env_amp = os.environ.get("STUDIO_PARALLAX_AMP", "").strip()
    adaptive = amplitude is None and not env_amp
    amp = amplitude if amplitude is not None else float(env_amp or DEFAULT_AMPLITUDE)
    if preset != "gentle-zoom":
        model_dir()  # fail fast with a clear message before any work
    cache = Path(cache_dir) if cache_dir else out.parent / ".depth"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    last: StageError | None = None
    for encoder in ("h264_videotoolbox", "libx264"):
        try:
            _render(image, tmp, frames, fmt, preset, amp, cache, encoder, adaptive)
            last = None
            break
        except StageError as exc:
            last = exc
    if last is not None:
        raise last
    tmp.replace(out)
    return out


def unload() -> None:
    global _model, _processor, _device
    device = _device
    _model, _processor, _device = None, None, None
    gc.collect()
    torch = sys.modules.get("torch")
    if device == "mps" and torch is not None:
        torch.mps.empty_cache()
