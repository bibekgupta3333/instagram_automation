"""Render one layered scene (background plate + animated cut-out characters) to an MP4.

Per frame: the background is warped by the camera at reduced strength (parallax: it moves
less than the characters), contact shadows are multiplied in, characters are warped with
their feet-anchored motion composed with the full camera move, and a soft warm grade
(per-channel LUT + light vignette) is applied. Frames are piped to ffmpeg
(h264_videotoolbox, 30 fps, exact frame count). OpenCV + numpy only, deterministic.

  uv run --no-sync python -m scripts.kids_song.render_scene --episode EP.json --shot 6 \
      --out shot.mp4 [--format 9x16|16x9] [--frames-png DIR] [--characters DIR] ...
"""

from __future__ import annotations

import argparse
import math
import subprocess
import time
import zlib
from pathlib import Path

import numpy as np

from . import character_motion as cm
from .scenes import SceneSpec, stage_px

FPS = 30
BG_PARALLAX = 0.55  # background moves this fraction of the foreground camera move
PALETTE = {  # placeholder otter colours (BGR): fur, accent, belly
    "pebble": ((70, 130, 196), (170, 170, 60), (200, 228, 245)),
    "fern": ((40, 62, 104), (220, 170, 200), (205, 225, 240)),
    "mum": ((110, 128, 150), (120, 120, 245), (205, 225, 240)),
    "bloop": ((160, 225, 150), (160, 225, 150), (150, 240, 255)),
}


# ---------------------------------------------------------------- camera + grade


def ease(t: float) -> float:
    """Cosine ease with a 30% linear share (as studio/stages/motion.py): never stops dead."""
    t = min(1.0, max(0.0, t))
    return 0.3 * t + 0.7 * (0.5 - 0.5 * math.cos(math.pi * t))


def camera_state(move: str, t: float) -> tuple[float, float, float]:
    """(zoom, dx, dy) at t in [0, 1]; dx/dy are fractions of the frame's short side."""
    e = ease(t)
    c = e - 0.5
    if move == "push-in":
        return 1.0 + 0.075 * e, 0.0, -0.012 * c
    if move == "pull-out":
        return 1.075 - 0.075 * e, 0.0, 0.0
    if move == "pan-right":  # camera moves right: content drifts left
        return 1.05, -0.06 * c, 0.0
    if move == "pan-left":
        return 1.05, 0.06 * c, 0.0
    if move == "tilt-up":  # camera rises: content drifts down
        return 1.05, 0.0, 0.05 * c
    return 1.0 + 0.045 * e, 0.008 * c, 0.0  # gentle-zoom with a soft drift


def camera_matrix(move: str, t: float, w: int, h: int, strength: float = 1.0) -> np.ndarray:
    """2x3 affine for scene coords -> frame: zoom about the centre, then shift."""
    z, dx, dy = camera_state(move, t)
    z = 1.0 + (z - 1.0) * strength
    s = min(w, h)
    cx, cy = w / 2.0, h / 2.0
    return np.array([[z, 0.0, cx - z * cx + dx * s * strength],
                     [0.0, z, cy - z * cy + dy * s * strength]], np.float64)


def warm_lut() -> np.ndarray:
    """256x1x3 uint8 LUT (BGR): gentle S-curve, warm lift in red, a touch less blue."""
    x = np.arange(256, dtype=np.float32) / 255.0
    s = x + 0.06 * np.sin(2 * np.pi * x) * (0.5 - np.abs(x - 0.5)) * 2  # soft contrast
    s = 0.03 + 0.95 * s  # lifted blacks, soft whites
    b = np.clip(s * 0.96, 0, 1)
    g = np.clip(s * 1.0 + 0.005, 0, 1)
    r = np.clip(s * 1.03 + 0.015, 0, 1)
    return (np.stack([b, g, r], axis=-1)[:, None, :] * 255 + 0.5).astype(np.uint8)


def vignette(w: int, h: int, strength: float = 0.14) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = ((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2
    v = 1.0 - strength * np.clip(d - 0.35, 0, None) / 1.65
    return np.repeat(v[..., None], 3, axis=2).astype(np.float32)


# ---------------------------------------------------------------- placeholders


def placeholder_background(w: int, h: int, seed: int = 0) -> np.ndarray:
    """A drawn bathroom-ish plate: teal tiles, a round window, a tub. Marked mock upstream."""
    import cv2

    img = np.zeros((h, w, 3), np.uint8)
    top, bottom = np.array([214, 226, 176], np.float32), np.array([190, 205, 150], np.float32)
    ramp = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    img[:] = (top * (1 - ramp) + bottom * ramp).astype(np.uint8)
    floor = int(h * 0.8)
    img[floor:] = (175, 215, 240)
    tile = max(8, w // 14)
    for x in range(0, w, tile):
        cv2.line(img, (x, 0), (x, floor), (225, 235, 200), 1, cv2.LINE_AA)
    for y in range(0, floor, tile):
        cv2.line(img, (0, y), (w, y), (225, 235, 200), 1, cv2.LINE_AA)
    rng = np.random.default_rng(seed)
    wx = int(w * (0.3 + 0.4 * rng.random()))
    cv2.circle(img, (wx, int(h * 0.28)), int(min(w, h) * 0.13), (250, 240, 215), -1, cv2.LINE_AA)
    cv2.circle(img, (wx, int(h * 0.28)), int(min(w, h) * 0.13), (150, 190, 220), 4, cv2.LINE_AA)
    cv2.ellipse(img, (w // 2, int(h * 0.83)), (int(w * 0.42), int(h * 0.08)), 0, 0, 360,
                (250, 250, 250), -1, cv2.LINE_AA)
    return img


def placeholder_character(name: str, variant: str = "base", size: int = 512) -> np.ndarray:
    """A drawn RGBA otter/frog stand-in (base / blink / singing) for tests and dry renders."""
    import cv2

    key = name.lower()
    fur, accent, belly = PALETTE.get(key, ((150, 150, 150), (200, 120, 120), (230, 230, 230)))
    w, h = size, int(size * 1.4)
    img = np.zeros((h, w, 4), np.uint8)

    def ell(c, axes, col, img=img):
        cv2.ellipse(img, c, axes, 0, 0, 360, (*col, 255), -1, cv2.LINE_AA)

    frog = key == "bloop"
    if frog:
        ell((w // 2, int(h * 0.72)), (int(w * 0.42), int(h * 0.26)), fur)
        ell((w // 2, int(h * 0.80)), (int(w * 0.28), int(h * 0.15)), belly)
        eyes = [(int(w * 0.30), int(h * 0.48)), (int(w * 0.70), int(h * 0.48))]
        for e in eyes:
            ell(e, (int(w * 0.13), int(w * 0.13)), fur)
        er, mouth_c = int(w * 0.075), (w // 2, int(h * 0.66))
    else:
        ell((w // 2, int(h * 0.70)), (int(w * 0.36), int(h * 0.29)), accent)  # clothes/body
        ell((w // 2, int(h * 0.74)), (int(w * 0.20), int(h * 0.16)), belly)
        for ex in (0.27, 0.73):
            ell((int(w * ex), int(h * 0.12)), (int(w * 0.08), int(w * 0.08)), fur)
        ell((w // 2, int(h * 0.30)), (int(w * 0.33), int(h * 0.20)), fur)  # head
        ell((w // 2, int(h * 0.37)), (int(w * 0.16), int(h * 0.09)), belly)  # muzzle
        eyes = [(int(w * 0.38), int(h * 0.27)), (int(w * 0.62), int(h * 0.27))]
        er, mouth_c = int(w * 0.055), (w // 2, int(h * 0.40))
        ell((w // 2, int(h * 0.345)), (int(w * 0.035), int(w * 0.025)), (150, 140, 235))  # nose
    for ex, ey in eyes:
        if variant == "blink":
            cv2.ellipse(img, (ex, ey), (er, er // 2), 0, 0, 180, (40, 30, 40, 255), 4,
                        cv2.LINE_AA)
        else:
            ell((ex, ey), (er, er), (40, 30, 40))
            ell((ex - er // 3, ey - er // 3), (er // 3, er // 3), (255, 255, 255))
    if variant == "singing":
        ell(mouth_c, (int(w * 0.06), int(w * 0.07)), (70, 50, 150))
    else:
        cv2.ellipse(img, mouth_c, (int(w * 0.06), int(w * 0.03)), 0, 10, 170,
                    (60, 40, 90, 255), 4, cv2.LINE_AA)
    return img


# ---------------------------------------------------------------- render


def format_variant(path: str, w: int, h: int) -> str:
    """Plates come as `<stem>-9x16.png` / `<stem>-16x9.png`: use the one matching the frame."""
    want, other = ("9x16", "16x9") if h >= w else ("16x9", "9x16")
    p = Path(path)
    if other in p.name:
        alt = p.with_name(p.name.replace(other, want))
        if alt.is_file():
            return str(alt)
    return path


def _load_background(path: str | None, w: int, h: int, seed: int) -> np.ndarray:
    """Cover-fit the plate to w x h (BGR uint8)."""
    import cv2

    if not path:
        return placeholder_background(w, h, seed)
    path = format_variant(path, w, h)
    src = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if src is None:
        raise FileNotFoundError(f"cannot read background {path}")
    sh, sw = src.shape[:2]
    k = max(w / sw, h / sh)
    nw, nh = max(w, round(sw * k)), max(h, round(sh * k))
    interp = cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC
    src = cv2.resize(src, (nw, nh), interpolation=interp)
    x0, y0 = (nw - w) // 2, (nh - h) // 2
    return np.ascontiguousarray(src[y0:y0 + h, x0:x0 + w])


def build_rigs(scene: SceneSpec, w: int, h: int, frames: int, t0: float,
               words: list[tuple[float, float]], fps: int = FPS) -> list[cm.CharacterRig]:
    unit = stage_px(w, h)
    chars = sorted(scene.characters, key=lambda c: c.z)
    sources = []
    for c in chars:
        if c.source == "placeholder" or not c.base:
            srcs = {v: placeholder_character(c.name, v) for v in ("base", "blink", "singing")}
        else:
            srcs = {"base": cm.load_rgba(Path(c.base)),
                    **{k: cm.load_rgba(Path(v)) for k, v in c.layers.items()}}
        sources.append(srcs)
    # a crowded line-up must fit the frame width: shrink everyone together if needed
    fit = 1.0
    if len(chars) >= 2 and "close" not in scene.shot.lower():
        widths = []
        for c, srcs in zip(chars, sources, strict=True):
            x0, y0, x1, y1 = cm.alpha_bbox(srcs["base"][..., 3])
            widths.append(c.height * unit * (x1 - x0) / max(1, y1 - y0))
        fit = min(1.0, 0.92 * w / max(1.0, sum(widths) * 0.85))
    rigs = []
    for c, srcs in zip(chars, sources, strict=True):
        rig = cm.build_rig(c.name, srcs, c.height * unit * fit, c.x * w, c.feet_y * h, c.flip)
        blinks = cm.blink_mask(frames, cm.blink_schedule(frames / fps, c.name, fps, t0))
        mouth = (cm.mouth_from_words(words, frames, t0, fps) if c.singing
                 else np.zeros(frames, bool))
        rig.blink_mask, rig.singing_mask = blinks, mouth
        rig.track = cm.layer_track(frames, blinks, mouth, "blink" in rig.layers,
                                   "singing" in rig.layers)
        rigs.append(rig)
    return rigs


def render_frames(scene: SceneSpec, w: int, h: int, frames: int, *, t0: float, bpm: float,
                  beat_offset: float = 0.0, words: list[tuple[float, float]] | None = None,
                  fps: int = FPS):
    """Yield BGR uint8 frames. t0 is the song time of the first frame."""
    import cv2

    cv2.setNumThreads(3)
    seed = zlib.crc32(f"{scene.index}:{scene.camera}".encode()) & 0xFFFF
    # the plate gets a margin so camera moves never reveal an edge
    margin = math.ceil(0.08 * max(w, h))
    bw, bh = w + 2 * margin, h + 2 * margin
    plate = _load_background(scene.background, bw, bh, seed)
    rigs = build_rigs(scene, w, h, frames, t0, words or [], fps)
    lut, vig = warm_lut(), vignette(w, h)
    for i in range(frames):
        u = i / max(frames - 1, 1)
        t = t0 + i / fps
        bg_cam = camera_matrix(scene.camera, u, w, h, BG_PARALLAX)
        mb = bg_cam.copy()
        mb[:, 2] -= mb[:, :2] @ np.array([margin, margin], np.float64)  # plate -> frame
        frame = cv2.warpAffine(plate, mb, (w, h), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REFLECT).astype(np.float32)
        cam = camera_matrix(scene.camera, u, w, h, 1.0)
        poses = [cm.pose_at(r.name, t, bpm, beat_offset, bool(r.singing_mask[i])) for r in rigs]
        for r, p in zip(rigs, poses, strict=True):  # shadows first: they sit on the floor
            base = r.layers["base"]
            fx, fy = cam @ np.array([r.x + p.dx * r.height, r.feet_y, 1.0])
            zoom = cam[0, 0]
            lift = p.lift / max(cm.style_for(r.name).bob * 1.25, 1e-6)
            draw_w = base.size[0] * p.sx * zoom * 0.82
            cm.draw_shadow(frame, fx, fy - 0.004 * r.height, draw_w, min(1.0, lift))
        for r, p in zip(rigs, poses, strict=True):
            layer = r.layers.get(r.track[i], r.layers["base"])
            m = cm.character_matrix(layer, p, r.x, r.feet_y, r.height, cam)
            cm.composite_layer(frame, layer, m)
        out = np.clip(frame * vig, 0, 255).astype(np.uint8)
        yield cv2.LUT(out, lut)


def encoder_args(w: int, h: int, encoder: str) -> list[str]:
    if encoder == "h264_videotoolbox":
        rate = "14M" if w * h >= 1_000_000 else "4M"
        return ["-c:v", "h264_videotoolbox", "-b:v", rate, "-profile:v", "high",
                "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16"]


def render_scene(scene: SceneSpec, size: tuple[int, int], frames: int, out: Path, *,
                 t0: float, bpm: float, beat_offset: float = 0.0,
                 words: list[tuple[float, float]] | None = None, fps: int = FPS,
                 encoder: str = "h264_videotoolbox", frames_png: Path | None = None,
                 png_every: int = 0) -> dict:
    """Render exactly `frames` frames to `out` (MP4, yuv420p, no audio)."""
    import cv2

    w, h = size
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.mp4")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "3",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
           "-an", *encoder_args(w, h, encoder), "-pix_fmt", "yuv420p", "-r", str(fps),
           "-frames:v", str(frames), "-movflags", "+faststart", str(tmp)]
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for i, frame in enumerate(render_frames(scene, w, h, frames, t0=t0, bpm=bpm,
                                                beat_offset=beat_offset, words=words,
                                                fps=fps)):
            if frames_png is not None and png_every and i % png_every == 0:
                frames_png.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(frames_png / f"frame-{i:04d}.png"), frame)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"render_scene: ffmpeg ({encoder}) failed: {err.strip()[-600:]}")
    tmp.replace(out)
    return {"path": str(out), "width": w, "height": h, "frames": frames,
            "seconds": round(time.monotonic() - started, 2)}


def main(argv: list[str] | None = None) -> int:
    from . import episode, scenes

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", type=Path, required=True)
    ap.add_argument("--shot", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--format", choices=("9x16", "16x9"), default="9x16")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--characters", type=Path, default=scenes.CHARACTERS_DIR)
    ap.add_argument("--backgrounds", type=Path, default=scenes.BACKGROUNDS_DIR)
    ap.add_argument("--renders", type=Path)
    ap.add_argument("--frames-png", type=Path, help="also write every 10th frame as PNG")
    args = ap.parse_args(argv)
    tl = episode.load(args.episode)
    shot = tl.shots[args.shot]
    scene = scenes.plan_scene(shot, tl, args.characters, args.backgrounds, args.renders)
    w, h = (1080, 1920) if args.format == "9x16" else (1920, 1080)
    w, h = (max(16, round(w * args.scale / 2) * 2), max(16, round(h * args.scale / 2) * 2))
    words = [(n.start, n.start + n.dur) for n in tl.notes if n.sung]
    rep = render_scene(scene, (w, h), round(shot.dur * FPS), args.out, t0=shot.start,
                       bpm=tl.bpm, words=words, frames_png=args.frames_png, png_every=10)
    print(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
