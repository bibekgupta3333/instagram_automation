"""Scene plan: episode shots -> layered scene specs (background, characters, camera).

Characters come from the locked pack (`assets/characters/<name>/{front,three_quarter,side,
expr_*}.png`, transparent cut-outs, optional card.yaml) or from a per-shot klein render
(`<out>/renders/shot-XX-<name>.png`, optional `shot-XX-<name>-singing.png` /
`-eyes_closed.png`). A per-shot render wins; otherwise the closest pack pose is used.
Backgrounds come from `<out>/renders/shot-XX-bg.png`, else the best-matching plate in
`assets/backgrounds/<brand>/`. Missing art falls back to drawn placeholders, which mark the
scene `mock` (do not publish).

Positions are normalised (x, feet_y in 0..1 of the frame; height in units of the frame's
"stage size", see `stage_px`) so one spec renders at 9:16 and 16:9.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .episode import Shot, Timeline

REPO = Path(__file__).resolve().parents[2]
CHARACTERS_DIR = REPO / "assets" / "characters"
BACKGROUNDS_DIR = REPO / "assets" / "backgrounds" / "plinkety-pond"
CAST = ("Mum", "Fern", "Pebble", "Bloop")
# relative standing heights (Mum tallest, Bloop smallest); card.yaml `relative_height` wins
HEIGHTS = {"mum": 1.0, "fern": 0.80, "pebble": 0.60, "bloop": 0.24}
DEFAULT_SINGER = "Pebble"
POSES = ("front", "three_quarter", "side")
# framing -> stage scale of a height-1.0 character; ground line (feet y)
FRAMING = {
    "wide": (0.58, 0.86), "medium wide": (0.70, 0.88), "medium": (0.80, 0.90),
    "two-shot": (0.80, 0.90), "medium close-up": (1.02, 0.95), "close-up": (1.25, 1.02),
    "over-the-shoulder": (1.0, 0.98),
}
CUTAWAY_WORDS = ("cutaway", "macro", "top-down")


@dataclass
class CharacterSpec:
    name: str
    base: str                  # image path of the chosen pose / render
    source: str                # "render", "pack" or "placeholder"
    pose: str                  # front / three_quarter / side / render / placeholder
    layers: dict[str, str] = field(default_factory=dict)  # 'blink' / 'singing' -> path
    x: float = 0.5             # feet x, fraction of frame width
    feet_y: float = 0.9        # fraction of frame height
    height: float = 0.6        # stage units (see stage_px)
    z: int = 0                 # draw order, higher = in front
    flip: bool = False
    singing: bool = False


@dataclass
class SceneSpec:
    index: int                 # shot index (-1 for the title card)
    shot: str
    start: float
    end: float
    background: str | None     # None -> drawn placeholder plate
    camera: str
    characters: list[CharacterSpec]
    mock: bool = False
    title_card: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def stage_px(width: int, height: int) -> float:
    """Pixel height of one stage unit: the frame height, limited by width so a 9:16 frame
    doesn't make the cast too wide to fit side by side."""
    return float(min(height, width * 1.25))


# ---------------------------------------------------------------- shot parsing


def characters_in(shot: Shot) -> list[str]:
    """Cast members named in the prompt, in order of first mention."""
    found = []
    for name in CAST:
        m = re.search(rf"\b{name}\b", shot.prompt)
        if m:
            found.append((m.start(), name))
    return [n for _, n in sorted(found)]


def framing(shot_type: str) -> tuple[float, float]:
    st = shot_type.lower().strip()
    for key in sorted(FRAMING, key=len, reverse=True):
        if key in st:
            return FRAMING[key]
    return FRAMING["medium"]


def camera_for(shot: Shot) -> str:
    """Camera move from the prompt's camera sentence, else from the shot type."""
    p = shot.prompt.lower()
    if "push-in" in p or "push in" in p:
        return "push-in"
    if "pan right" in p:
        return "pan-right"
    if "pan left" in p:
        return "pan-left"
    if "tilt" in p or "tilt" in shot.shot.lower() or "floats slowly up" in p:
        return "tilt-up"
    if "pull back" in p or "pull-out" in p:
        return "pull-out"
    st = shot.shot.lower()
    if "close" in st:
        return "push-in"
    if "wide" in st:
        return "pan-right"
    return "gentle-zoom"


def is_cutaway(shot: Shot) -> bool:
    return any(w in shot.shot.lower() for w in CUTAWAY_WORDS) and not characters_in(shot)


# ---------------------------------------------------------------- asset lookup


def _card(char_dir: Path) -> dict[str, Any]:
    path = char_dir / "card.yaml"
    if not path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - a broken card must not stop a render
        return {}


def char_dir(name: str, characters_dir: Path) -> Path:
    for cand in (name.lower(), name, name.lower().replace(" ", "-")):
        if (characters_dir / cand).is_dir():
            return characters_dir / cand
    return characters_dir / name.lower()


def relative_height(name: str, characters_dir: Path) -> float:
    card = _card(char_dir(name, characters_dir))
    for key in ("relative_height", "height_ratio", "scale"):
        v = card.get(key)
        if isinstance(v, (int, float)) and 0.05 <= float(v) <= 2.0:
            return float(v)
    return HEIGHTS.get(name.lower(), 0.6)


def desired_pose(shot: Shot, name: str, focus: bool, n_chars: int) -> str:
    st = shot.shot.lower()
    if "side" in st:
        return "side"
    if "over-the-shoulder" in st and not focus:
        return "three_quarter"
    if n_chars > 1 and not focus:
        return "three_quarter"
    return "front"


def expression_hint(shot: Shot, name: str) -> str | None:
    """An expression keyword from the part of the prompt describing this character."""
    p = shot.prompt
    m = re.search(rf"\b{name}\b", p)
    if not m:
        return None
    seg = p[m.start():m.start() + 420].lower()
    nxt = [seg.find(f" {o.lower()},", 1) for o in CAST if o != name]
    nxt = [i for i in nxt if i > 0]
    seg = seg[:min(nxt)] if nxt else seg
    for word, expr in (("giggl", "happy"), ("laugh", "happy"), ("happily", "happy"),
                       ("smile", "happy"), ("surpris", "surprised"), ("curious", "curious"),
                       ("yawn", "sleepy"), ("cuddle", "happy")):
        if word in seg:
            return expr
    return None


def pack_layers(cdir: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted(cdir.glob("*.png"))} if cdir.is_dir() else {}


def pick_character(shot: Shot, name: str, focus: bool, n_chars: int, characters_dir: Path,
                   renders_dir: Path | None) -> tuple[str, str, str, dict[str, str]]:
    """(base path, source, pose, extra layers) for one character in one shot."""
    if renders_dir is not None and shot.index >= 0:
        stem = f"shot-{shot.index:02d}-{name.lower()}"
        render = renders_dir / f"{stem}.png"
        if render.is_file():
            extra = {}
            for key, suffix in (("singing", "singing"), ("blink", "eyes_closed")):
                p = renders_dir / f"{stem}-{suffix}.png"
                if p.is_file():
                    extra[key] = str(p)
            return str(render), "render", "render", extra
    files = pack_layers(char_dir(name, characters_dir))
    if not files:
        return "", "placeholder", "placeholder", {}
    want = desired_pose(shot, name, focus, n_chars)
    order = [want] + [p for p in ("front", "three_quarter", "side") if p != want]
    pose = next((p for p in order if p in files), None)
    expr = expression_hint(shot, name)
    base = files.get(pose) if pose else next(iter(files.values()))
    if pose == "front" and expr and f"expr_{expr}" in files:
        base, pose = files[f"expr_{expr}"], f"front+{expr}"
    extra = {}
    # expression layers are drawn front-on: swap only when the base is a front pose
    if (pose or "").startswith("front"):
        if "expr_singing" in files:
            extra["singing"] = str(files["expr_singing"])
        if "expr_eyes_closed" in files:
            extra["blink"] = str(files["expr_eyes_closed"])
    return str(base), "pack", pose or "front", extra


def pick_background(shot: Shot, backgrounds_dir: Path, renders_dir: Path | None) -> str | None:
    if renders_dir is not None and shot.index >= 0:
        p = renders_dir / f"shot-{shot.index:02d}-bg.png"
        if p.is_file():
            return str(p)
    plates = sorted([*backgrounds_dir.glob("*.png"), *backgrounds_dir.glob("*.jpg")]) \
        if backgrounds_dir.is_dir() else []
    if not plates:
        return None
    text = f"{shot.prompt} {shot.shot}".lower()

    def score(p: Path) -> tuple[int, int]:
        toks = [t for t in re.split(r"[-_ .]+", p.stem.lower()) if len(t) > 2]
        hits = sum(1 for t in toks if t in text)
        close = int(("close" in p.stem.lower()) == ("close" in shot.shot.lower()))
        return hits, close

    return str(max(plates, key=lambda p: (score(p), -plates.index(p))))


# ---------------------------------------------------------------- layout


def layout(names: list[str], focus: str | None, shot_type: str,
           heights: dict[str, float]) -> list[tuple[str, float, float, float, int]]:
    """(name, x, feet_y, height, z): spread across the frame, tallest at the back."""
    if not names:
        return []
    scale, ground = framing(shot_type)
    close = "close" in shot_type.lower()
    if close and len(names) > 2:  # a close-up holds the focus and one partner
        names = [focus or names[0]] + [n for n in names if n != (focus or names[0])][:1]
    n = len(names)
    # left-to-right: keep mention order but put the tallest in the middle for 3+
    order = sorted(names, key=lambda m: -heights[m]) if n >= 3 else list(names)
    if n >= 3:
        mid = order.pop(0)
        order.insert(n // 2, mid)
    span = {1: (0.5,), 2: (0.34, 0.66), 3: (0.22, 0.5, 0.78), 4: (0.16, 0.39, 0.62, 0.85)}
    xs = span.get(n) or tuple(0.12 + 0.76 * i / (n - 1) for i in range(n))
    tallest = max(heights[m] for m in names)
    out = []
    for name, x in zip(order, xs, strict=True):
        h = heights[name] * scale
        if close and name == (focus or names[0]):
            h *= 1.12
        if close and n == 1:
            x = 0.5
        # smaller characters stand a touch nearer the camera (lower feet), drawn in front
        nearer = 0.035 * (1.0 - heights[name] / tallest)
        z = round((1.0 - heights[name] / tallest) * 100) + (5 if name == focus else 0)
        out.append((name, x, ground + nearer, h, z))
    return out


def _singer(shot: Shot, names: list[str], tl: Timeline) -> str | None:
    raw = (tl.raw.get("shots") or [])
    if 0 <= shot.index < len(raw) and "singer" in raw[shot.index]:
        s = raw[shot.index]["singer"]
        return s if s in names else None
    if not names:
        return None
    return DEFAULT_SINGER if DEFAULT_SINGER in names else names[0]


def plan_scene(shot: Shot, tl: Timeline, characters_dir: Path = CHARACTERS_DIR,
               backgrounds_dir: Path = BACKGROUNDS_DIR, renders_dir: Path | None = None,
               cast: list[str] | None = None, title_card: bool = False) -> SceneSpec:
    names = cast if cast is not None else ([] if is_cutaway(shot) else characters_in(shot))
    singer = None if title_card else _singer(shot, names, tl)
    focus = singer if singer in names else (names[0] if names else None)
    heights = {n: relative_height(n, characters_dir) for n in names}
    chars, mock = [], False
    for name, x, feet_y, h, z in layout(names, focus, shot.shot, heights):
        base, source, pose, extra = pick_character(shot, name, name == focus, len(names),
                                                   characters_dir, renders_dir)
        mock = mock or source == "placeholder"
        # side / three-quarter cut-outs face right: flip those right of centre to face in
        flip = pose in ("side", "three_quarter") and x > 0.5
        chars.append(CharacterSpec(name, base, source, pose, extra, x, feet_y, h, z, flip,
                                   singing=name == singer))
    chars.sort(key=lambda c: c.z)
    bg = pick_background(shot, backgrounds_dir, renders_dir)
    cam = "gentle-zoom" if title_card else camera_for(shot)
    return SceneSpec(shot.index, shot.shot, shot.start, shot.end, bg, cam, chars,
                     mock=mock or bg is None, title_card=title_card)


def plan_scenes(tl: Timeline, characters_dir: Path = CHARACTERS_DIR,
                backgrounds_dir: Path = BACKGROUNDS_DIR,
                renders_dir: Path | None = None) -> list[SceneSpec]:
    """One scene per timeline shot. A shot with index -1 is the title card: the first
    shot's background with the whole family lined up."""
    scenes = []
    for shot in tl.shots:
        if shot.index < 0:
            first = next((s for s in tl.shots if s.index >= 0), shot)
            proxy = Shot(-1, shot.start_beat, shot.end_beat, shot.start, shot.end,
                         first.prompt, "", "wide", shot.seed)
            sc = plan_scene(proxy, tl, characters_dir, backgrounds_dir, None,
                            cast=["Pebble", "Mum", "Fern", "Bloop"], title_card=True)
            sc.background = pick_background(first, backgrounds_dir, renders_dir)
            sc.mock = any(c.source == "placeholder" for c in sc.characters) or not sc.background
            scenes.append(sc)
        else:
            scenes.append(plan_scene(shot, tl, characters_dir, backgrounds_dir, renders_dir))
    return scenes


def scene_seed(scene: SceneSpec) -> int:
    return zlib.crc32(f"{scene.index}:{scene.shot}:{scene.camera}".encode()) & 0x7FFFFFFF
