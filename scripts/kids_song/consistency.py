"""Character consistency check: DINOv2-small cosine similarity + HSV signature colours.

    .venv/bin/python -m scripts.kids_song.consistency --character pebble IMG [IMG ...]
    .venv/bin/python -m scripts.kids_song.consistency --baseline          # the pack itself

For each candidate image it reports the cosine similarity (CLS embedding of
facebook/dinov2-small, Apache-2.0, offline) against every reference PNG in
`assets/characters/<name>/`, the max and mean, and for each signature colour in that
character's `card.yaml` the fraction of foreground pixels within a hue/saturation/value
window of it. RGBA references and candidates are composited on #FFF8EC first, and only
opaque pixels count for the colour check (all pixels for an opaque RGB image).

`--baseline` scores every pack image against the *other* images of the same character
(leave-one-out) and against every other character, which gives the threshold: a new
render should score at least about the lowest same-character similarity, and clearly
above the highest cross-character one. Light: DINOv2-small is ~88 MB and runs on CPU/MPS.
"""

from __future__ import annotations

import argparse
import colorsys
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "assets" / "characters"
MODEL = "facebook/dinov2-small"
REVISION = "ed25f3a31f01632728cabb09d1542f84ab7b0056"
BG = (255, 248, 236)  # #FFF8EC
HUE_TOL, SAT_TOL, VAL_TOL = 0.045, 0.30, 0.35  # hue in 0..1 (0.045 ~ 16 deg)
MIN_COLOUR_FRAC = 0.01


def load_rgb(path: Path) -> tuple[Image.Image, np.ndarray]:
    """(composited RGB image, boolean foreground mask)."""
    im = Image.open(path)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        alpha = np.asarray(im)[..., 3]
        bg = Image.new("RGB", im.size, BG)
        bg.paste(im, mask=im.split()[3])
        return bg, alpha > 128
    rgb = im.convert("RGB")
    return rgb, np.ones((rgb.height, rgb.width), bool)


class Embedder:
    def __init__(self) -> None:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import torch
        from transformers import AutoImageProcessor, AutoModel

        self.torch = torch
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.proc = AutoImageProcessor.from_pretrained(MODEL, revision=REVISION)
        self.model = AutoModel.from_pretrained(MODEL, revision=REVISION).to(self.device).eval()

    def __call__(self, images: list[Image.Image]) -> np.ndarray:
        with self.torch.no_grad():
            x = self.proc(images=images, return_tensors="pt").to(self.device)
            cls = self.model(**x).last_hidden_state[:, 0].float().cpu().numpy()
        return cls / np.linalg.norm(cls, axis=1, keepdims=True)


def hex_hsv(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(r, g, b)


def colour_fractions(img: Image.Image, mask: np.ndarray, signature: dict[str, str]) -> dict:
    hsv = np.asarray(img.convert("HSV"), dtype=np.float32) / 255.0
    fg = hsv[mask]
    out = {}
    for label, hexv in signature.items():
        th, ts, tv = hex_hsv(hexv)
        dh = np.abs(fg[:, 0] - th)
        dh = np.minimum(dh, 1 - dh)
        ok = (dh <= HUE_TOL) & (np.abs(fg[:, 1] - ts) <= SAT_TOL) & (np.abs(fg[:, 2] - tv) <= VAL_TOL)
        out[label] = round(float(ok.mean()) if len(fg) else 0.0, 4)
    return out


def card(name: str) -> dict:
    p = ASSETS / name / "card.yaml"
    if not p.exists():
        return {}
    import yaml
    return yaml.safe_load(p.read_text()) or {}


def pack(name: str) -> list[Path]:
    return sorted((ASSETS / name).glob("*.png"))


def score(name: str, candidates: list[Path], emb: Embedder) -> list[dict]:
    refs = pack(name)
    if not refs:
        raise SystemExit(f"no reference PNGs in {ASSETS / name}")
    sig = card(name).get("signature_colours", {})
    ref_vec = emb([load_rgb(p)[0] for p in refs])
    rows = []
    for c in candidates:
        img, mask = load_rgb(c)
        v = emb([img])[0]
        sims = ref_vec @ v
        keep = [i for i, r in enumerate(refs) if r.resolve() != c.resolve()]
        s = sims[keep] if keep else sims
        cols = colour_fractions(img, mask, sig)
        rows.append({
            "candidate": str(c), "character": name,
            "dino_max": round(float(s.max()), 4), "dino_mean": round(float(s.mean()), 4),
            "per_ref": {refs[i].stem: round(float(sims[i]), 4) for i in keep},
            "colours": cols,
            "colours_ok": all(f >= MIN_COLOUR_FRAC for f in cols.values()),
        })
    return rows


def baseline(emb: Embedder) -> dict:
    names = sorted(p.name for p in ASSETS.iterdir() if p.is_dir() and pack(p.name))
    vecs = {n: emb([load_rgb(p)[0] for p in pack(n)]) for n in names}
    out: dict = {"characters": {}, "cross": {}}
    for n in names:
        v = vecs[n]
        sims = v @ v.T
        k = len(v)
        off = sims[~np.eye(k, dtype=bool)] if k > 1 else np.array([1.0])
        loo_max = [float(np.max(np.delete(sims[i], i))) for i in range(k)] if k > 1 else [1.0]
        sig = card(n).get("signature_colours", {})
        cols = {p.stem: colour_fractions(*load_rgb(p), sig) for p in pack(n)}
        out["characters"][n] = {
            "images": [p.stem for p in pack(n)],
            "same_pair_min": round(float(off.min()), 4),
            "same_pair_mean": round(float(off.mean()), 4),
            "loo_max_min": round(min(loo_max), 4),
            "colours": cols,
        }
        for m in names:
            if m <= n:
                continue
            cross = v @ vecs[m].T
            out["cross"][f"{n}~{m}"] = {"max": round(float(cross.max()), 4),
                                         "mean": round(float(cross.mean()), 4)}
    same_min = min(c["same_pair_min"] for c in out["characters"].values())
    loo_min = min(c["loo_max_min"] for c in out["characters"].values())
    cross_max = max((c["max"] for c in out["cross"].values()), default=0.0)
    out["summary"] = {"same_pair_min": same_min, "loo_max_min": loo_min,
                      "cross_max": cross_max,
                      "suggested_threshold_dino_max": round((loo_min + cross_max) / 2, 3)
                      if loo_min > cross_max else None}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--character")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--json", type=Path, help="also write the result here")
    ap.add_argument("images", nargs="*", type=Path)
    a = ap.parse_args(argv)
    emb = Embedder()
    if a.baseline:
        res = baseline(emb)
    elif a.character and a.images:
        res = score(a.character, a.images, emb)
    else:
        ap.error("give --baseline, or --character NAME and images")
    text = json.dumps(res, indent=2)
    print(text)
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
