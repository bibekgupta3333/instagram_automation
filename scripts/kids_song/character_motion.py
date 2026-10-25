"""Layered cut-out character motion (OpenCV + numpy only, deterministic).

Each character is a transparent cut-out (RGBA). Per frame we pick a layer (neutral, blink,
singing), then place it with an affine transform anchored at the feet:

  * squash-and-stretch on the song's beat grid with constant area (sx * sy == 1): squashed
    as it lands on the beat, stretched at the top of the bob
  * a bob (vertical hop) on the same grid, a slow sway and a small rotation
  * an idle breathing scale (a few tenths of a percent, slower than the beat)
  * blinks every 2-4 s (3 frames of `expr_eyes_closed`) and mouth switching to
    `expr_singing` while that character's words are being sung
  * a soft contact shadow under the feet that shrinks and fades as the character rises

All times are seconds on the song timeline, so every shot of a character stays in phase.
"""

from __future__ import annotations

import math
import random
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

FPS = 30
BLINK_FRAMES = 3
BLINK_MIN, BLINK_MAX = 2.0, 4.0
MOUTH_LEAD, MOUTH_TAIL, MOUTH_MIN_GAP = 0.03, 0.05, 2  # seconds, seconds, frames


@dataclass(frozen=True)
class MotionStyle:
    """Per-character amplitudes (fractions of the character's height unless noted)."""

    bob: float = 0.018         # hop height
    squash: float = 0.035      # sy = 1 -/+ squash at landing/peak
    sway: float = 0.012        # horizontal sway
    rotate_deg: float = 1.6    # peak rotation
    breath: float = 0.006      # idle breathing scale
    beats_per_hop: float = 1.0


STYLES = {
    "mum": MotionStyle(bob=0.010, squash=0.018, sway=0.008, rotate_deg=1.0, breath=0.005,
                       beats_per_hop=2.0),
    "fern": MotionStyle(bob=0.015, squash=0.028, sway=0.011, rotate_deg=1.4),
    "pebble": MotionStyle(bob=0.022, squash=0.042, sway=0.013, rotate_deg=1.8),
    "bloop": MotionStyle(bob=0.05, squash=0.07, sway=0.01, rotate_deg=2.5, breath=0.012),
}


def style_for(name: str) -> MotionStyle:
    return STYLES.get(name.lower(), MotionStyle())


def phase_offset(name: str) -> float:
    """A small deterministic beat-phase offset per character so they don't move in lockstep."""
    return (zlib.crc32(name.lower().encode()) % 1000) / 1000.0 * 0.18


# ---------------------------------------------------------------- pose maths


def beat_phase(t: float, bpm: float, beat_offset: float = 0.0, beats_per_hop: float = 1.0,
               extra: float = 0.0) -> float:
    """Phase in [0, 1) of the hop cycle; 0 is the landing on a beat."""
    return ((t - beat_offset) * bpm / 60.0 / beats_per_hop + extra) % 1.0


def squash_stretch(phase: float, amount: float) -> tuple[float, float]:
    """(sx, sy) with sx * sy == 1: squashed at the landing (phase 0), stretched mid-hop."""
    sy = 1.0 - amount * math.cos(2.0 * math.pi * phase)
    return 1.0 / sy, sy


def bob(phase: float, height: float) -> float:
    """Upward offset (same unit as `height`): 0 on the beat, `height` mid-hop."""
    return height * math.sin(math.pi * phase)


@dataclass(frozen=True)
class Pose:
    sx: float
    sy: float
    rot_deg: float
    dx: float       # fraction of character height, + right
    lift: float     # fraction of character height, + up
    phase: float

    @property
    def area(self) -> float:
        return self.sx * self.sy


def pose_at(name: str, t: float, bpm: float, beat_offset: float = 0.0, singing: bool = False,
            style: MotionStyle | None = None) -> Pose:
    st = style or style_for(name)
    k = 1.25 if singing else 1.0  # the singer bounces a little more
    ph = beat_phase(t, bpm, beat_offset, st.beats_per_hop, phase_offset(name))
    sx, sy = squash_stretch(ph, st.squash * k)
    breath = 1.0 + st.breath * math.sin(2.0 * math.pi * t / 3.4 + phase_offset(name) * 20)
    sx, sy = sx * breath, sy * (1.0 + (breath - 1.0) * 0.5)
    bar = 4.0 * 60.0 / bpm  # sway over two bars, rotation over one, both smooth
    sway = st.sway * math.sin(2.0 * math.pi * (t - beat_offset) / (2 * bar) + phase_offset(name))
    rot = st.rotate_deg * k * math.sin(2.0 * math.pi * (t - beat_offset) / bar
                                       + 7 * phase_offset(name))
    return Pose(sx, sy, rot, sway, bob(ph, st.bob * k), ph)


# ---------------------------------------------------------------- layer schedules


def blink_schedule(seconds: float, seed: str | int, fps: int = FPS,
                   t0: float = 0.0) -> list[int]:
    """Start frames (relative to t0) of each blink. Blinks are seeded on the song timeline,
    so a character blinks at the same moments whichever shot it is in."""
    rng = random.Random(f"blink:{seed}")
    starts, t = [], rng.uniform(0.6, BLINK_MAX)
    end = t0 + seconds
    while t < end:
        if t >= t0:
            starts.append(round((t - t0) * fps))
        t += rng.uniform(BLINK_MIN, BLINK_MAX)
    return starts


def blink_mask(frames: int, starts: list[int], length: int = BLINK_FRAMES) -> np.ndarray:
    mask = np.zeros(frames, bool)
    for s in starts:
        mask[max(0, s):max(0, s + length)] = True
    return mask


def mouth_from_words(words: list[tuple[float, float]], frames: int, t0: float = 0.0,
                     fps: int = FPS) -> np.ndarray:
    """Per-frame 'mouth open' from (start, end) word timings on the song timeline.

    Open from slightly before a word to slightly before its end; a closed gap of at least
    MOUTH_MIN_GAP frames separates consecutive words so the mouth visibly moves."""
    open_ = np.zeros(frames, bool)
    for a, b in words:
        f0 = math.floor((a - MOUTH_LEAD - t0) * fps)
        f1 = math.ceil((b - MOUTH_TAIL - t0) * fps)
        f1 = max(f1, f0 + 2)
        if f1 <= 0 or f0 >= frames:
            continue
        open_[max(0, f0):min(frames, f1)] = True
    # close the mouth for a couple of frames before every word, so back-to-back words read
    for a, _b in words:
        f0 = math.floor((a - MOUTH_LEAD - t0) * fps)
        if 0 < f0 < frames:
            open_[max(0, f0 - MOUTH_MIN_GAP):f0] = False
    return open_


def mouth_from_envelope(audio: np.ndarray, sr: int, frames: int, t0: float = 0.0,
                        fps: int = FPS, on: float = 0.35, off: float = 0.22) -> np.ndarray:
    """Fallback: per-frame mouth state from an isolated vocal's RMS envelope (hysteresis)."""
    mono = audio.mean(axis=1) if audio.ndim == 2 else audio
    hop = sr / fps
    rms = np.zeros(frames, np.float32)
    for i in range(frames):
        a = int((t0 * fps + i) * hop)
        seg = mono[a:a + int(hop)]
        rms[i] = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2))) if seg.size else 0.0
    peak = float(np.percentile(rms, 95)) or 1.0
    env, state, out = rms / peak, False, np.zeros(frames, bool)
    for i, v in enumerate(env):
        state = v >= (off if state else on)
        out[i] = state
    return out


def layer_track(frames: int, blinks: np.ndarray, mouth: np.ndarray, has_blink: bool,
                has_sing: bool) -> list[str]:
    """Per-frame layer name: 'singing' while the mouth is open, else 'blink', else 'base'."""
    out = []
    for i in range(frames):
        if has_sing and mouth[i]:
            out.append("singing")
        elif has_blink and blinks[i]:
            out.append("blink")
        else:
            out.append("base")
    return out


# ---------------------------------------------------------------- layers and drawing


def load_rgba(path: Path) -> np.ndarray:
    import cv2

    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"cannot read character layer {path}")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    return img


@dataclass
class Layer:
    """A cropped, premultiplied cut-out with its feet anchor (bottom centre), in pixels."""

    rgb: np.ndarray    # float32 premultiplied BGR
    alpha: np.ndarray  # float32 0..1
    anchor: tuple[float, float]

    @property
    def size(self) -> tuple[int, int]:
        return self.alpha.shape[1], self.alpha.shape[0]


def alpha_bbox(alpha: np.ndarray, thresh: int = 8) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(alpha > thresh)
    if not len(xs):
        return 0, 0, alpha.shape[1], alpha.shape[0]
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def prepare_layer(rgba: np.ndarray, scale: float) -> Layer:
    """Crop to the opaque box, resize once by `scale`, premultiply, find the feet."""
    import cv2

    x0, y0, x1, y1 = alpha_bbox(rgba[..., 3])
    crop = rgba[y0:y1, x0:x1]
    w, h = max(2, round(crop.shape[1] * scale)), max(2, round(crop.shape[0] * scale))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    crop = cv2.resize(crop, (w, h), interpolation=interp)
    a = crop[..., 3].astype(np.float32) / 255.0
    rgb = crop[..., :3].astype(np.float32) * a[..., None]
    # feet: centre of the opaque pixels in the bottom 6% of the figure
    band = a[int(h * 0.94):]
    cols = np.nonzero(band.max(axis=0) > 0.3)[0] if band.size else np.array([])
    ax = float(cols.mean()) if len(cols) else w / 2.0
    pad = 2  # zero border so warps never smear the edge pixels
    rgb = cv2.copyMakeBorder(rgb, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    a = cv2.copyMakeBorder(a, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    return Layer(rgb, a, (ax + pad, h + pad - 0.5))


@dataclass
class CharacterRig:
    """Everything needed to draw one character in one shot."""

    name: str
    layers: dict[str, Layer]              # 'base' and optionally 'blink', 'singing'
    x: float                              # feet x in frame pixels (camera at rest)
    feet_y: float                         # feet y in frame pixels
    height: float                         # target height in pixels
    flip: bool = False
    singing_mask: np.ndarray | None = None
    blink_mask: np.ndarray | None = None
    track: list[str] = field(default_factory=list)


def build_rig(name: str, sources: dict[str, Path | np.ndarray], height_px: float, x: float,
              feet_y: float, flip: bool = False) -> CharacterRig:
    """Load and size each layer variant. Variants on the same canvas size as the base share
    its scale (expression swaps then don't change size); others are scaled to its height."""
    import cv2

    raw = {k: (load_rgba(v) if isinstance(v, (str, Path)) else v) for k, v in sources.items()}
    if flip:
        raw = {k: cv2.flip(v, 1) for k, v in raw.items()}
    base = raw["base"]
    _, by0, _, by1 = alpha_bbox(base[..., 3])
    k_base = height_px / max(1, by1 - by0)
    layers = {}
    for key, img in raw.items():
        if img.shape[:2] == base.shape[:2]:
            k = k_base
        else:
            _, y0, _, y1 = alpha_bbox(img[..., 3])
            k = height_px / max(1, y1 - y0)
        layers[key] = prepare_layer(img, k)
    return CharacterRig(name, layers, x, feet_y, height_px, flip)


def character_matrix(layer: Layer, pose: Pose, x: float, feet_y: float, height: float,
                     cam: np.ndarray) -> np.ndarray:
    """2x3 affine: layer pixels -> frame pixels (feet-anchored scale/rotate, then camera)."""
    ax, ay = layer.anchor
    th = math.radians(pose.rot_deg)
    c, s = math.cos(th), math.sin(th)
    # R * S, then translate the anchor to the placed feet position
    a = np.array([[c * pose.sx, -s * pose.sy], [s * pose.sx, c * pose.sy]], np.float64)
    dest = np.array([x + pose.dx * height, feet_y - pose.lift * height])
    t = dest - a @ np.array([ax, ay])
    local = np.vstack([np.hstack([a, t[:, None]]), [0, 0, 1]])
    cam3 = np.vstack([cam, [0, 0, 1]])
    return (cam3 @ local)[:2]


def composite_layer(frame: np.ndarray, layer: Layer, m: np.ndarray) -> None:
    """Warp a premultiplied layer into its bounding ROI on a float32 frame (in place)."""
    import cv2

    h, w = frame.shape[:2]
    lw, lh = layer.size
    corners = np.array([[0, 0, 1], [lw, 0, 1], [0, lh, 1], [lw, lh, 1]], np.float64).T
    pts = m @ corners
    x0, y0 = max(0, math.floor(pts[0].min()) - 1), max(0, math.floor(pts[1].min()) - 1)
    x1, y1 = min(w, math.ceil(pts[0].max()) + 1), min(h, math.ceil(pts[1].max()) + 1)
    if x1 <= x0 or y1 <= y0:
        return
    mr = m.copy()
    mr[0, 2] -= x0
    mr[1, 2] -= y0
    size = (x1 - x0, y1 - y0)
    rgb = cv2.warpAffine(layer.rgb, mr, size, flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    a = cv2.warpAffine(layer.alpha, mr, size, flags=cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    roi = frame[y0:y1, x0:x1]
    roi *= (1.0 - a)[..., None]
    roi += rgb


def draw_shadow(frame: np.ndarray, cx: float, cy: float, width: float, lift: float,
                strength: float = 0.32) -> None:
    """Soft elliptical contact shadow (multiplied into a float32 frame, in place).
    `lift` 0..1 (fraction of the max bob) shrinks and fades it as the character rises."""
    import cv2

    h, w = frame.shape[:2]
    rx = max(2.0, width * 0.5 * (1.0 - 0.25 * lift))
    ry = max(1.5, rx * 0.16)
    pad = int(ry * 3 + 4)
    x0, x1 = max(0, int(cx - rx - pad)), min(w, int(cx + rx + pad) + 1)
    y0, y1 = max(0, int(cy - ry - pad)), min(h, int(cy + ry + pad) + 1)
    if x1 <= x0 or y1 <= y0:
        return
    m = np.zeros((y1 - y0, x1 - x0), np.float32)
    cv2.ellipse(m, (round(cx - x0), round(cy - y0)), (round(rx), round(ry)), 0, 0, 360, 1.0, -1,
                cv2.LINE_AA)
    k = int(max(3, ry * 1.6)) | 1
    m = cv2.GaussianBlur(m, (k, k), 0)
    m *= strength * (1.0 - 0.45 * lift)
    frame[y0:y1, x0:x1] *= (1.0 - m)[..., None]
