"""Plinkety Pond character pack: klein candidates, locked reference views, cut-outs, plates.

Run every heavy subcommand through the resource guard (one guarded job at a time):

    .venv/bin/python scripts/run_guarded.py -- .venv/bin/python -m scripts.kids_song.character_pack \
        candidates --name pebble
    ... refs --name pebble --pick output/kids/characters/candidates/pebble/hero_seed_1103.png
    ... cutout --name pebble          # BiRefNet_lite -> assets/characters/pebble/*.png
    ... plates                        # assets/backgrounds/plinkety-pond/*.png

Models (pulled via the manifest only; never `-q`, never a base repo):
  mflux-community/flux2-klein-4b-mflux-q4 (Apache-2.0) with `--base-model flux2-klein-4b`,
  ZhengPeng7/BiRefNet_lite (MIT). Everything runs with HF_HUB_OFFLINE=1.

Verified against mflux 0.23 `mflux-generate-flux2-edit --help` (2026-10-23): reference images
go in `--image-paths A [B ...]`; `--low-ram`, `--vae-tile-size`, `--mlx-cache-limit-gb`,
`--base-model` exist; guidance must be 1.0 for distilled klein; several `--seed`s write
`<stem>_seed_<n>.png`. Descriptions are doc 27 §1 verbatim. No text in any image.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "output" / "kids" / "characters"
ASSETS = REPO / "assets" / "characters"
PLATES = REPO / "assets" / "backgrounds" / "plinkety-pond"
BIN = REPO / ".venv" / "bin"
EP_RENDERS = REPO / "output" / "kids" / "plinkety-pond-ep01" / "renders"
# chosen by the user 2026-10-23 (output/kids/characters/picks.json)
PICKS = {
    "pebble": "output/kids/characters/candidates/pebble/hero_otter_seed_1108.png",
    "fern": "output/kids/characters/candidates/fern/hero_seed_2103.png",
    "mum": "output/kids/characters/candidates/mum/hero_seed_3104.png",
    "bloop": "output/kids/characters/candidates/bloop/hero_seed_4104.png",
}
# DINOv2-small CLS cosine vs the pick, calibrated on Pebble's pack (front 0.99, expressions
# 0.96-0.98, three-quarter 0.95, side 0.85; other characters' picks score 0.59-0.73)
DINO_MIN = {"front": 0.90, "three_quarter": 0.84, "side": 0.78, "expr": 0.90, "shot": 0.72}
BODY_DIFF_MAX = 12.0  # mean |expr - front| (0-255 grey) below the head: the pose must not move
MAX_RETRIES = 3
# the compositor (scenes.py) assumes turned cut-outs face RIGHT and flips those right of centre
MIRROR = {"pebble": ("three_quarter", "side")}  # Pebble's turns were made facing left
RELATIVE_HEIGHT = {"mum": 1.0, "fern": 0.8, "pebble": 0.6, "bloop": 0.24}
KLEIN =["--model", "mflux-community/flux2-klein-4b-mflux-q4", "--base-model", "flux2-klein-4b",
         "--steps", "4", "--guidance", "1.0", "--low-ram", "--vae-tile-size", "256",
         "--mlx-cache-limit-gb", "0.25"]
BIREFNET = "ZhengPeng7/BiRefNet_lite"
BIREFNET_REV = "aa62cd87eafb9cc43056d08ef3615a14628b831d"
MAX_PNG_BYTES = 1_500_000
BG_HEX = "#FFF8EC"

STYLE = ("soft 3D clay-and-plush cartoon, rounded shapes, warm key light from upper left, soft "
         "ambient occlusion, cream backdrop, palette #5EC8C0 #FFD25E #FF8C7A, no text, no logos")
PLAIN = (f"plain flat warm-white {BG_HEX} background, nothing else in the picture, soft contact "
         "shadow only, soft 3D clay-and-plush cartoon, rounded shapes, warm key light from upper "
         "left, soft ambient occlusion, no text, no letters, no logos")

# doc 27 §1, verbatim (Pebble in the "dressed" variant: teal is Pebble's fixed colour)
CHARACTERS: dict[str, dict] = {
    "pebble": {
        "display": "Pebble",
        "desc": ("Pebble, a small toddler otter with a round chubby body and a big round head, "
                 "caramel-brown fur, a cream heart-shaped tummy patch, a little three-tuft curl on "
                 "top of the head, big round dark-brown eyes with white shine, a tiny pink nose, "
                 "wearing teal dungarees with one round yellow button"),
        "pose": "standing",
        "seed_base": 1100,
        "signature": {"teal dungarees": "#3FB8B0", "caramel fur": "#C68A4E",
                      "yellow button": "#FFD25E"},
    },
    "fern": {
        "display": "Fern",
        "desc": ("Fern, an older otter sister, taller and slimmer than Pebble, dark chocolate-brown "
                 "fur, a cream face, a lilac bow on her left ear, a lilac-and-white striped jumper"),
        "pose": "standing",
        "seed_base": 2100,
        "signature": {"lilac bow and jumper": "#B79AD8", "chocolate fur": "#5A3A26"},
    },
    "mum": {
        "display": "Mum",
        "desc": ("Mum, a grown-up otter with soft grey-brown fur, a cream muzzle, a gentle smile, a "
                 "coral-pink cardigan with round wooden buttons and the sleeves rolled up"),
        "pose": "standing",
        "seed_base": 3100,
        "signature": {"coral cardigan": "#FF8C7A", "grey-brown fur": "#8A7A6A"},
    },
    "bloop": {
        "display": "Bloop",
        "desc": ("Bloop, a tiny round mint-green pet frog the size of a teacup, a pale yellow belly, "
                 "big shiny black eyes"),
        "pose": "sitting",
        "seed_base": 4100,
        "signature": {"mint green skin": "#8FDCB0", "pale yellow belly": "#F3E79A"},
    },
}

VIEWS = {  # name -> (references, instruction)
    "front": ("pick", ("Show this exact same character, full body, {pose}, facing the viewer "
              "straight on, front view, neutral friendly expression with a small closed-mouth "
              "smile, arms relaxed, centred with space around it.")),
    "three_quarter": ("pick+front", ("Show this exact same character, full body, {pose}, turned "
                      "to a three-quarter view facing right of frame, neutral friendly expression, "
                      "centred with space around it.")),
    "side": ("pick+front", ("Show this exact same character, full body, {pose}, in side profile "
             "facing right of frame, neutral friendly expression, centred with space around "
             "it.")),
    "expr_happy": ("front", ("Keep this exact same character, pose, framing, size and position. "
                   "Only change the face: a big happy open smile with bright eyes.")),
    "expr_singing": ("front", ("Keep this exact same character, pose, framing, size and position. "
                     "Only change the mouth: open round mouth, singing a happy song, eyes open.")),
    "expr_eyes_closed": ("front", ("Keep this exact same character, pose, framing, size and "
                         "position. Only change the eyes: both eyes gently closed as in a blink, "
                         "relaxed small smile.")),
}

PLATE_SPECS = {  # doc 27 §1 setting + shots 1-2 (hallway) and 3-24 (bathroom)
    "otter-bathroom": ("Empty cosy bathroom in an otter cottage, pale teal tiles, a round window "
                       "with soft daylight and a glimpse of a calm pond, a round white bathtub with "
                       "shallow warm water and soft white foam bubbles, a buttercup-yellow towel on "
                       "a hook, a soft round bathmat on a warm wooden floor, clear open floor space "
                       "in the foreground, eye-level view, no characters, no animals, no people. "),
    "otter-bathroom-close": ("Close view inside a cosy otter cottage bathroom: the curved side of a "
                             "round white bathtub with shallow warm water and soft white foam "
                             "bubbles across the lower part of the picture, pale teal tiles and a "
                             "buttercup-yellow towel on a hook softly out of focus behind, a round "
                             "window glowing with daylight, no characters, no animals, no "
                             "people. "),
    "otter-hallway":("Empty cosy otter cottage hallway with a round wooden front door, cream "
                      "walls, a soft teal rug on a warm wooden floor, a little coat hook with a "
                      "yellow raincoat, a small potted plant, clear open floor space in the "
                      "foreground, eye-level view, no characters, no animals, no people. "),
}
# a bathroom with NO tub, for per-shot backgrounds where the character render brings the tub
EXTRA_PLATES = {
    "otter-bathroom-notub": ("Empty cosy bathroom in an otter cottage, pale teal tiles, a round "
                             "window with soft daylight and a glimpse of a calm pond, a "
                             "buttercup-yellow towel on a hook, a small wooden stool, a soft round "
                             "bathmat on a warm wooden floor, wide clear open floor space in the "
                             "lower half, no bathtub, eye-level view, no characters, no animals, "
                             "no people. "),
}
PLATE_STYLE = ("Soft rounded 3D clay and plush animation style, bright warm soft lighting, warm key "
               "light from upper left, soft ambient occlusion, pastel teal #5EC8C0, buttercup "
               "yellow #FFD25E and coral #FF8C7A palette, calm toddler-friendly mood, no text, no "
               "letters, no logos, no watermark")


def _log(record: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "timings.jsonl").open("a") as f:
        f.write(json.dumps(record) + "\n")


def mflux(cli: str, prompt: str, out: Path, seeds: list[int], width: int, height: int,
          refs: list[Path] | None = None, label: str = "") -> None:
    """One mflux process per seed (a yellow-pressure stop loses at most one image; reruns
    skip images that exist). mflux writes `<stem>_seed_<n>.png`, or `out` for one seed."""
    for seed in seeds:
        path = out if len(seeds) == 1 else out.with_stem(f"{out.stem}_seed_{seed}")
        if path.exists():
            continue
        _mflux_one(cli, prompt, path, seed, width, height, refs, label)


def _mflux_one(cli: str, prompt: str, out: Path, seed: int, width: int, height: int,
               refs: list[Path] | None, label: str) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    seeds = [seed]
    cmd = ["/usr/bin/time", "-l", str(BIN / cli), *KLEIN, "--width", str(width), "--height",
           str(height), "--seed", str(seed), "--prompt", prompt, "--output", str(out)]
    if refs:
        cmd += ["--image-paths", *map(str, refs)]
    assert "-q" not in cmd and "--quantize" not in cmd
    env = {**os.environ, "HF_HUB_OFFLINE": "1"}
    t0 = time.time()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    wall = time.time() - t0
    err = proc.stdout + proc.stderr
    if proc.returncode != 0:
        sys.stderr.write(err[-2000:])
        raise SystemExit(f"mflux failed ({label})")
    its = [float(x) for x in re.findall(r"([\d.]+)s/it", err)]
    peak = re.search(r"(\d+)\s+peak memory footprint", err)
    mlx = re.search(r"Peak MLX memory: ([\d.]+) GB", err)
    rec = {"label": label, "cli": cli, "seeds": seeds, "size": f"{width}x{height}",
           "refs": [str(r) for r in refs or []], "wall_s": round(wall, 1),
           "s_per_image": round(wall / len(seeds), 1),
           "s_per_step_last": its[-1] if its else None,
           "peak_footprint_gb": round(int(peak.group(1)) / 1e9, 2) if peak else None,
           "peak_mlx_gb": float(mlx.group(1)) if mlx else None, "time": t0}
    _log(rec)
    print(json.dumps(rec), flush=True)
    return rec


def cmd_candidates(name: str) -> None:
    c = CHARACTERS[name]
    d = OUT / "candidates" / name
    sb = c["seed_base"]
    hero = (f"Character design hero image of one single character, full body, {c['pose']}, facing "
            f"the viewer, centred, the whole body in frame with margin all around. {c['desc']}. "
            f"{STYLE}")
    sheet = (f"Character turnaround sheet of one single character drawn three times side by side "
             f"on one cream backdrop: front view, three-quarter view and side view, full body, "
             f"{c['pose']}, evenly spaced, identical design in every view. {c['desc']}. {STYLE}")
    mflux("mflux-generate-flux2", hero, d / "hero.png", [sb + i for i in range(1, 5)], 768, 1344,
          label=f"{name} hero t2i")
    mflux("mflux-generate-flux2", sheet, d / "sheet.png", [sb + 5, sb + 6], 1344, 768,
          label=f"{name} sheet t2i")
    (d / "prompts.json").write_text(json.dumps({"hero": hero, "sheet": sheet}, indent=2))


class Gate:
    """DINOv2 similarity to the pick, plus (expressions) a check that the body did not move."""

    def __init__(self, pick: Path) -> None:
        from .consistency import Embedder, load_rgb

        self.load_rgb = load_rgb
        self.emb = Embedder()
        self.pick_vec = self.emb([load_rgb(pick)[0]])[0]

    def dino(self, path: Path) -> float:
        return round(float(self.emb([self.load_rgb(path)[0]])[0] @ self.pick_vec), 4)

    @staticmethod
    def body_diff(path: Path, front: Path) -> float:
        """Mean grey difference below the top 45% of the image (the head), at low res."""
        import numpy as np
        from PIL import Image

        def grey(p: Path):
            im = Image.open(p).convert("L").resize((192, 336), Image.BILINEAR)
            return np.asarray(im, dtype=np.float32)

        a, b = grey(path), grey(front)
        y = int(a.shape[0] * 0.45)
        return round(float(np.abs(a[y:] - b[y:]).mean()), 2)

    def check(self, view: str, path: Path, front: Path | None) -> dict:
        kind = "expr" if view.startswith("expr_") else view
        res = {"dino": self.dino(path), "dino_min": DINO_MIN.get(kind, DINO_MIN["shot"])}
        ok = res["dino"] >= res["dino_min"]
        if kind == "expr" and front is not None:
            res["body_diff"] = self.body_diff(path, front)
            ok = ok and res["body_diff"] <= BODY_DIFF_MAX
        res["ok"] = bool(ok)
        return res


def gated_edit(name: str, label: str, prompt: str, refs: list[Path], out: Path, seed: int,
               gate: Gate, view: str, front: Path | None, size=(768, 1344)) -> dict:
    """klein edit at `seed`; if the gate rejects it, retry seed+1 .. seed+MAX_RETRIES and keep
    the first that passes (else the best-scoring attempt, flagged)."""
    import shutil

    tries = []
    for attempt in range(MAX_RETRIES + 1):
        s = seed + attempt
        tmp = out.parent / "attempts" / f"{out.stem}_seed_{s}.png"
        if not tmp.exists():
            mflux("mflux-generate-flux2-edit", prompt, tmp, [s], *size, refs=refs,
                  label=f"{label} seed {s}")
        res = {"seed": s, "path": str(tmp.relative_to(REPO)), **gate.check(view, tmp, front)}
        tries.append(res)
        print(json.dumps({"gate": label, **res}), flush=True)
        if res["ok"]:
            break
    best = next((t for t in tries if t["ok"]), None) or max(tries, key=lambda t: t["dino"])
    shutil.copyfile(REPO / best["path"], out)
    return {"seed": best["seed"], "dino": best["dino"], "passed": best["ok"],
            "body_diff": best.get("body_diff"), "attempts": tries,
            "refs": [str(r.relative_to(REPO)) for r in refs], "prompt": prompt}


def cmd_refs(name: str, pick: Path, only: list[str] | None) -> None:
    c = CHARACTERS[name]
    d = OUT / "refs" / name
    sb = c["seed_base"] + 50
    done: dict[str, Path] = {"pick": pick}
    gate: Gate | None = None
    meta = d / "seeds.json"
    for i, (view, (refkey, instr)) in enumerate(VIEWS.items()):
        out = d / f"{view}.png"
        if out.exists():
            done[view] = out
            continue
        if only and view not in only:
            continue
        refs = [done[k] for k in refkey.split("+")]
        prompt = f"{instr.format(pose=c['pose'])} {c['desc']}. {PLAIN}"
        gate = gate or Gate(pick)
        d.mkdir(parents=True, exist_ok=True)
        rec = gated_edit(name, f"{name} {view} edit ({len(refs)} ref)", prompt, refs, out,
                         sb + i, gate, view, done.get("front"))
        done[view] = out
        seeds = json.loads(meta.read_text()) if meta.exists() else {}
        seeds[view] = rec
        meta.write_text(json.dumps(seeds, indent=2))


def cmd_score(name: str, pick: Path) -> None:
    """Score an existing refs folder against the pick (for refs made before the gate)."""
    d = OUT / "refs" / name
    gate = Gate(pick)
    meta = d / "seeds.json"
    seeds = json.loads(meta.read_text()) if meta.exists() else {}
    for view in VIEWS:
        p = d / f"{view}.png"
        if p.exists():
            res = gate.check(view, p, d / "front.png" if view.startswith("expr_") else None)
            seeds.setdefault(view, {}).update({"dino": res["dino"], "passed": res["ok"],
                                               "body_diff": res.get("body_diff")})
            print(view, json.dumps(res))
    meta.write_text(json.dumps(seeds, indent=2))


def cmd_picks() -> None:
    rec = {
        "chosen": "chosen by user 2026-10-23",
        "note": ("The user picked these designs from output/kids/characters/candidates/. "
                 "An earlier agent-suggested shortlist (2026-10-23) is superseded."),
        "picks": {n: {"pick": p, "seed": int(re.search(r"seed_(\d+)", p).group(1)),
                      "chosen_by": "user", "date": "2026-10-23"} for n, p in PICKS.items()},
    }
    (OUT / "picks.json").write_text(json.dumps(rec, indent=2) + "\n")


# ------------------------------------------------------------------ BiRefNet_lite cut-outs


def _shim_remote_deps() -> None:
    """BiRefNet's remote code imports timm.layers and kornia.filters.laplacian, which are not
    project dependencies. Provide the four tiny functions it uses (inference only)."""
    import torch
    import torch.nn.functional as F
    from torch import nn

    if "timm" not in sys.modules:
        try:
            import timm
        except ImportError:
            class DropPath(nn.Module):
                def __init__(self, drop_prob=0.0):
                    super().__init__()
                    self.drop_prob = drop_prob

                def forward(self, x):
                    return x  # eval only

            def to_2tuple(x):
                return tuple(x) if isinstance(x, (tuple, list)) else (x, x)

            timm = types.ModuleType("timm")
            layers = types.ModuleType("timm.layers")
            layers.DropPath, layers.to_2tuple = DropPath, to_2tuple
            layers.trunc_normal_ = nn.init.trunc_normal_
            timm.layers = layers
            sys.modules["timm"], sys.modules["timm.layers"] = timm, layers
    if "kornia" not in sys.modules:
        try:
            import kornia
        except ImportError:
            def laplacian(x, kernel_size=5, border_type="reflect", normalized=True):
                k = torch.ones(kernel_size, kernel_size, dtype=x.dtype, device=x.device)
                k[kernel_size // 2, kernel_size // 2] = 1 - kernel_size ** 2
                if normalized:
                    k = k / k.abs().sum()
                p = kernel_size // 2
                xp = F.pad(x, (p, p, p, p), mode=border_type)
                return F.conv2d(xp, k.expand(x.shape[1], 1, -1, -1), groups=x.shape[1])

            kornia = types.ModuleType("kornia")
            filters = types.ModuleType("kornia.filters")
            filters.laplacian = laplacian
            kornia.filters = filters
            sys.modules["kornia"], sys.modules["kornia.filters"] = kornia, filters


class Cutter:
    def __init__(self) -> None:
        os.environ["HF_HUB_OFFLINE"] = "1"
        _shim_remote_deps()
        import torch
        from transformers import AutoModelForImageSegmentation

        self.torch = torch
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model = AutoModelForImageSegmentation.from_pretrained(
            BIREFNET, revision=BIREFNET_REV, trust_remote_code=True).to(self.device).eval()

    def alpha(self, img):
        import numpy as np
        torch = self.torch
        x = np.asarray(img.convert("RGB").resize((1024, 1024)), dtype=np.float32) / 255.0
        x = (x - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
        t = torch.from_numpy(x.transpose(2, 0, 1)[None].astype(np.float32)).to(self.device)
        with torch.no_grad():
            pred = self.model(t)[-1].sigmoid().float().cpu()[0, 0].numpy()
        from PIL import Image
        m = Image.fromarray((pred * 255).clip(0, 255).astype("uint8")).resize(img.size,
                                                                           Image.BILINEAR)
        return m


def _save_small(rgba, path: Path) -> None:
    from PIL import Image
    im = rgba
    while True:
        im.save(path, optimize=True)
        if path.stat().st_size <= MAX_PNG_BYTES:
            return
        im = im.resize((int(im.width * 0.85), int(im.height * 0.85)), Image.LANCZOS)


def _lab_match(src, ref, src_m, ref_m):
    """Per-channel LAB mean/std transfer of `src` (RGB uint8) onto `ref`'s foreground stats."""
    import cv2
    import numpy as np

    a = cv2.cvtColor(src, cv2.COLOR_RGB2LAB).astype(np.float32)
    b = cv2.cvtColor(ref, cv2.COLOR_RGB2LAB).astype(np.float32)
    for ch in range(3):
        sa, sb = a[..., ch][src_m], b[..., ch][ref_m]
        a[..., ch] = (a[..., ch] - sa.mean()) * (sb.std() / max(sa.std(), 1e-3)) + sb.mean()
    return cv2.cvtColor(np.clip(a, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)


def fuse_face(front, front_a, expr, expr_a) -> tuple:
    """Registered expression layer: the front image with only the changed face region taken
    from the (colour-matched) expression image, feathered. Blink / singing swaps then change
    nothing but the eyes and mouth. Inputs RGB uint8 and alpha uint8 at the same size."""
    import cv2
    import numpy as np

    fm, em = front_a > 128, expr_a > 128
    both = fm & em
    e = _lab_match(expr, front, both, both)
    ys = np.nonzero(fm.any(axis=1))[0]
    y0, y1 = int(ys[0]), int(ys[-1])
    head_end = y0 + int(0.5 * (y1 - y0))
    d = np.abs(e.astype(np.float32) - front.astype(np.float32)).mean(axis=2)
    d = cv2.GaussianBlur(d, (0, 0), 3)
    m = ((d > 18) & (fm | em)).astype(np.uint8)
    m[head_end:] = 0
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m)
    keep = np.zeros_like(m)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= 150:
            keep[lab == i] = 1
    keep = cv2.dilate(keep, np.ones((21, 21), np.uint8))
    w = cv2.GaussianBlur(keep.astype(np.float32), (0, 0), 7)[..., None]
    rgb = (front.astype(np.float32) * (1 - w) + e.astype(np.float32) * w)
    alpha = front_a.astype(np.float32) * (1 - w[..., 0]) + expr_a.astype(np.float32) * w[..., 0]
    return (np.clip(rgb, 0, 255).astype(np.uint8), np.clip(alpha, 0, 255).astype(np.uint8),
            round(float(keep.mean()), 4))


def _bbox(mask, thresh: int = 24):
    import numpy as np
    pts = np.argwhere(mask > thresh)
    return pts[:, 1].min(), pts[:, 0].min(), pts[:, 1].max(), pts[:, 0].max()


def cmd_cutout(name: str, mirror: tuple[str, ...] | None = None) -> None:
    import numpy as np
    from PIL import Image

    mirror = MIRROR.get(name, ()) if mirror is None else mirror
    src = OUT / "refs" / name
    dst = ASSETS / name
    dst.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    cutter = Cutter()
    imgs, alphas = {}, {}
    for view in VIEWS:
        p = src / f"{view}.png"
        if p.exists():
            im = Image.open(p).convert("RGB")
            imgs[view] = np.asarray(im).copy()
            alphas[view] = np.asarray(cutter.alpha(im)).copy()
    fused = {}
    for view in ("expr_singing", "expr_eyes_closed"):  # swapped in per frame: must register
        if view in imgs and "front" in imgs:
            imgs[view], alphas[view], frac = fuse_face(imgs["front"], alphas["front"],
                                                       imgs[view], alphas[view])
            fused[view] = frac
    if "expr_happy" in imgs and "front" in imgs:  # a base pose, not a swap: colour-match only
        m = (alphas["expr_happy"] > 128) & (alphas["front"] > 128)
        imgs["expr_happy"] = _lab_match(imgs["expr_happy"], imgs["front"], m, m)
    group = [v for v in imgs if v == "front" or v.startswith("expr_")]
    gb = [_bbox(alphas[v]) for v in group]
    group_box = (min(b[0] for b in gb), min(b[1] for b in gb), max(b[2] for b in gb),
                 max(b[3] for b in gb))
    info = {}
    for view, rgb in imgs.items():
        x0, y0, x1, y1 = group_box if view in group else _bbox(alphas[view])
        pad = 12
        h, w = alphas[view].shape
        box = (max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad))
        rgba = Image.fromarray(np.dstack([rgb, alphas[view]]), "RGBA").crop(box)
        if view in mirror:
            rgba = rgba.transpose(Image.FLIP_LEFT_RIGHT)
        out = dst / f"{view}.png"
        _save_small(rgba, out)
        info[view] = {"crop_box": [int(v) for v in box], "bytes": out.stat().st_size,
                      "size": list(Image.open(out).size), "mirrored": view in mirror,
                      **({"face_patch_frac": fused[view]} if view in fused else {})}
    _log({"label": f"{name} birefnet cutout", "n": len(imgs), "wall_s": round(time.time() - t0, 1)})
    write_card(name, info)
    print(json.dumps(info, indent=1))


def write_card(name: str, info: dict) -> None:
    import yaml

    c = CHARACTERS[name]
    meta = OUT / "refs" / name / "seeds.json"
    seeds = json.loads(meta.read_text()) if meta.exists() else {}
    card = {
        "name": c["display"],
        "source_pick": PICKS[name],
        "source_pick_chosen": "chosen by user 2026-10-23",
        "description": c["desc"],
        "description_tokens": [t.strip() for t in re.split(r",| wearing | with ", c["desc"])
                               if t.strip()],
        "signature_colours": c["signature"],
        "relative_height": RELATIVE_HEIGHT[name],
        "pose": c["pose"],
        "facing": "front views face the viewer; three_quarter and side face RIGHT of frame",
        "views": {v: {"file": f"{v}.png", "seed": seeds.get(v, {}).get("seed"),
                      "dino_vs_pick": seeds.get(v, {}).get("dino"),
                      "gate_passed": seeds.get(v, {}).get("passed"),
                      **{k: info[v][k] for k in ("size", "mirrored", "face_patch_frac")
                         if k in info[v]}}
                  for v in info},
        "model": "mflux-community/flux2-klein-4b-mflux-q4 edit, 4 steps, 768x1344, guidance 1.0",
        "cutout": f"{BIREFNET}@{BIREFNET_REV[:8]}",
        "notes": ("expr_singing / expr_eyes_closed = front with only the changed face region "
                  "from the expression render (colour-matched, feathered), on front's crop box, "
                  "so the compositor can swap them per frame"),
    }
    (ASSETS / name / "card.yaml").write_text(yaml.safe_dump(card, sort_keys=False, width=100))


def cmd_plates(only: list[str] | None = None) -> None:
    tmp = OUT / "plates"
    specs = {**PLATE_SPECS, **EXTRA_PLATES}
    for i, (stem, scene) in enumerate(specs.items()):
        if only and stem not in only:
            continue
        prompt = scene + PLATE_STYLE
        for j, (w, h, tag) in enumerate(((768, 1344, "9x16"), (1344, 768, "16x9"))):
            seeds = [5100 + 10 * i + 2 * j, 5101 + 10 * i + 2 * j]
            mflux("mflux-generate-flux2", prompt, tmp / f"{stem}-{tag}.png", seeds, w, h,
                  label=f"plate {stem} {tag}")


def cmd_plate_finish(choices: list[str]) -> None:
    """`stem-tag=seed` -> Real-ESRGAN x4 to 1080x1920 / 1920x1080 in the plates folder
    (EXTRA_PLATES go to the episode renders folder instead: per-shot backgrounds only)."""
    sys.path.insert(0, str(REPO))
    from studio.stages.visuals import upscale

    for ch in choices:
        key, seed = ch.split("=")
        stem, tag = key.rsplit("-", 1)
        src = OUT / "plates" / f"{stem}-{tag}_seed_{seed}.png"
        size = (1080, 1920) if tag == "9x16" else (1920, 1080)
        dest_dir = EP_RENDERS / "plates" if stem in EXTRA_PLATES else PLATES
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / f"{stem}-{tag}.png"
        t0 = time.time()
        upscale.upscale_to(src, dst, *size)
        _log({"label": f"esrgan {stem}-{tag}", "wall_s": round(time.time() - t0, 1),
              "src": str(src.relative_to(REPO)), "bytes": dst.stat().st_size})
        print(dst, dst.stat().st_size, flush=True)
    upscale.unload()


def cmd_shot(index: int, name: str, action: str, seed: int, with_: list[str]) -> None:
    """Per-shot action pose: klein edit from the character's front + three_quarter refs
    (plus the front ref of any `with_` character drawn in the same picture)."""
    c = CHARACTERS[name]
    refs = [OUT / "refs" / name / "front.png", OUT / "refs" / name / "three_quarter.png"]
    others = ""
    for o in with_:
        refs.append(OUT / "refs" / o / "front.png")
        others += f" Together with {CHARACTERS[o]['desc']}, exactly as in the last reference."
    prompt = (f"Show this exact same character from the first two references: {action}.{others} "
              f"Whole bodies in frame, centred with space around. {c['desc']}. {PLAIN}")
    raw = EP_RENDERS / "raw" / f"shot-{index:02d}-{name}.png"
    raw.parent.mkdir(parents=True, exist_ok=True)
    rec = gated_edit(name, f"shot {index:02d} {name} ({len(refs)} ref)", prompt, refs, raw,
                     seed, Gate(REPO / PICKS[name]), "shot", None)
    rec.update({"shot": index, "character": name, "includes": [CHARACTERS[o]["display"]
                                                                for o in with_]})
    (raw.with_suffix(".json")).write_text(json.dumps(rec, indent=2))


def cmd_cutout_shots() -> None:
    import numpy as np
    from PIL import Image

    cutter = None
    for raw in sorted((EP_RENDERS / "raw").glob("shot-*.png")):
        out = EP_RENDERS / raw.name
        if out.exists():
            continue
        cutter = cutter or Cutter()
        t0 = time.time()
        im = Image.open(raw).convert("RGB")
        a = np.asarray(cutter.alpha(im))
        x0, y0, x1, y1 = _bbox(a)
        box = (max(0, x0 - 12), max(0, y0 - 12), min(im.width, x1 + 12), min(im.height, y1 + 12))
        rgba = Image.fromarray(np.dstack([np.asarray(im), a]), "RGBA").crop(box)
        _save_small(rgba, out)
        side = raw.with_suffix(".json")
        if side.exists():
            meta = json.loads(side.read_text())
            (out.with_suffix(".json")).write_text(json.dumps(
                {k: meta[k] for k in ("shot", "character", "includes", "seed", "dino", "passed")
                 if k in meta}, indent=2))
        _log({"label": f"birefnet {raw.name}", "wall_s": round(time.time() - t0, 1)})
        print(out, out.stat().st_size, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("candidates")
    s.add_argument("--name", choices=CHARACTERS, action="append", required=True)
    s = sub.add_parser("refs")
    s.add_argument("--name", choices=CHARACTERS, required=True)
    s.add_argument("--pick", type=Path, help="default: the user's pick (PICKS)")
    s.add_argument("--only", nargs="*")
    s = sub.add_parser("score")
    s.add_argument("--name", choices=CHARACTERS, required=True)
    s = sub.add_parser("cutout")
    s.add_argument("--name", choices=CHARACTERS, action="append", required=True)
    sub.add_parser("picks")
    s = sub.add_parser("plates")
    s.add_argument("--only", nargs="*")
    s = sub.add_parser("plate-finish")
    s.add_argument("choices", nargs="+", help="stem-tag=seed, e.g. otter-bathroom-9x16=5100")
    s = sub.add_parser("shot")
    s.add_argument("--index", type=int, required=True)
    s.add_argument("--name", choices=CHARACTERS, required=True)
    s.add_argument("--action", required=True)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--with", dest="with_", choices=CHARACTERS, action="append", default=[])
    sub.add_parser("cutout-shots")
    a = ap.parse_args()
    if a.cmd == "candidates":
        for n in a.name:
            cmd_candidates(n)
    elif a.cmd == "refs":
        cmd_refs(a.name, (a.pick or REPO / PICKS[a.name]).resolve(), a.only)
    elif a.cmd == "score":
        cmd_score(a.name, REPO / PICKS[a.name])
    elif a.cmd == "cutout":
        for n in a.name:
            cmd_cutout(n)
    elif a.cmd == "picks":
        cmd_picks()
    elif a.cmd == "plates":
        cmd_plates(a.only)
    elif a.cmd == "plate-finish":
        cmd_plate_finish(a.choices)
    elif a.cmd == "shot":
        cmd_shot(a.index, a.name, a.action, a.seed, a.with_)
    elif a.cmd == "cutout-shots":
        cmd_cutout_shots()


if __name__ == "__main__":
    main()
