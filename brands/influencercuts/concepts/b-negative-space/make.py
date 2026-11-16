"""InfluencerCuts concept B: negative-space mark, "The Cut Creator".

One solid disc. The cut-away space inside it is a head and shoulders (the
creator, the way every profile slot draws a person). One diagonal slice goes
through the neck and the two halves slide apart along the cut, like two clips
either side of an edit point. No letters, no play triangle, no speech bubble.

Run: .venv/bin/python make.py      (writes next to this file)
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SS = 4  # supersample factor

# ---- palette ---------------------------------------------------------------
TILE = (18, 23, 64)      # ink navy      #121740
TOP = (255, 200, 46)     # caption amber #FFC82E  (the piece that was cut off)
BODY = (255, 246, 226)   # cream         #FFF6E2
DARK = (15, 15, 15)      # YouTube dark UI, assumed #0F0F0F
WHITE = (255, 255, 255)
MONO = (255, 122, 26)    # one-colour logo, mid-tone orange #FF7A1A (reads on white and on dark)

# ---- geometry, all in units of the disc radius R, origin at disc centre -----
G = dict(
    head_r=0.27, head_y=-0.43,            # head (negative circle)
    sh_rx=0.56, sh_top=0.21, sh_bot=0.66,  # shoulders (negative dome, flat base)
    cut_y=-0.03, cut_deg=-22,              # slice line: through the neck, rising to the right
    gap=0.19,                              # width of the slice
    slide=0.34,                            # how far the halves slide along the cut
    max_r=0.43,                           # farthest mark pixel from canvas centre / canvas
)


def _disc(n: int, R: float, cx: float, cy: float) -> Image.Image:
    """Solid disc with the head-and-shoulders cut away."""
    m = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(m)
    d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=255)
    hr, hy = G["head_r"] * R, cy + G["head_y"] * R
    d.ellipse([cx - hr, hy - hr, cx + hr, hy + hr], fill=0)
    top, bot, rx = cy + G["sh_top"] * R, cy + G["sh_bot"] * R, G["sh_rx"] * R
    sh = Image.new("L", (n, n), 0)
    ds = ImageDraw.Draw(sh)
    ds.ellipse([cx - rx, top, cx + rx, top + 2 * (bot - top)], fill=255)
    ds.rectangle([0, bot, n, n], fill=0)
    return ImageChops.subtract(m, sh)


def _pieces(n: int, R: float, cx: float, cy: float):
    """Return (top_piece, bottom_piece) masks after the slice and the slide."""
    m = _disc(n, R, cx, cy)
    t = math.radians(G["cut_deg"])
    ux, uy = math.cos(t), math.sin(t)
    nx, ny = -uy, ux                      # normal, pointing to the lower side
    px, py = cx, cy + G["cut_y"] * R
    L = 4 * n
    side = Image.new("L", (n, n), 0)
    a, b = (px - L * ux, py - L * uy), (px + L * ux, py + L * uy)
    ImageDraw.Draw(side).polygon([a, b, (b[0] + nx * L, b[1] + ny * L), (a[0] + nx * L, a[1] + ny * L)], fill=255)
    low = ImageChops.multiply(m, side)
    top = ImageChops.multiply(m, ImageChops.invert(side))
    g, s = G["gap"] * R / 2, G["slide"] * R / 2

    def move(im, k):  # k=+1 lower piece, -1 upper piece
        return ImageChops.offset(im, round(k * (nx * g - ux * s)), round(k * (ny * g - uy * s)))

    return move(top, -1), move(low, 1)


def mark(n: int, max_r: float | None = None):
    """Masks at n px, optically centred on the bounding circle and scaled to max_r."""
    max_r = max_r or G["max_r"]
    R, cx, cy = 0.30 * n, n / 2, n / 2
    for _ in range(2):  # pass 1 measures, pass 2 corrects size and centre
        top, low = _pieces(n, R, cx, cy)
        ys, xs = np.nonzero(np.maximum(np.asarray(top), np.asarray(low)) > 127)
        bx, by = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
        far = np.hypot(xs - bx, ys - by).max()
        k = max_r * n / far
        cx, cy, R = n / 2 + (cx - bx) * k, n / 2 + (cy - by) * k, R * k
    return _pieces(n, R, cx, cy)


def render(px: int, tile=TILE, top=TOP, body=BODY, max_r=None) -> Image.Image:
    """tile=None gives a transparent RGBA image."""
    n = px * SS
    mt, ml = mark(n, max_r)
    img = Image.new("RGBA", (n, n), (*tile, 255) if tile else (0, 0, 0, 0))
    for m, col in ((mt, top), (ml, body)):
        img.paste(Image.new("RGBA", (n, n), (*col, 255)), (0, 0), m)
    return img.resize((px, px), Image.LANCZOS)


def circle_crop(img: Image.Image, d: int) -> Image.Image:
    big = img.resize((d * SS, d * SS), Image.LANCZOS).convert("RGBA")
    m = Image.new("L", big.size, 0)
    ImageDraw.Draw(m).ellipse([0, 0, big.size[0] - 1, big.size[1] - 1], fill=255)
    big.putalpha(ImageChops.multiply(big.getchannel("A"), m))
    return big.resize((d, d), Image.LANCZOS)


def font(sz):
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(p, sz)
        except OSError:
            pass
    return ImageFont.load_default()


def contrast(a, b):
    def lum(c):
        v = [x / 255 for x in c]
        v = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in v]
        return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def sheet(avatar: Image.Image, logo: Image.Image) -> Image.Image:
    W, H = 1760, 1000
    ink = (40, 40, 40)
    s = Image.new("RGB", (W, H), (236, 236, 232))
    d = ImageDraw.Draw(s)
    s.paste(avatar.resize((760, 760), Image.LANCZOS).convert("RGB"), (60, 60))
    d.text((60, 838), "InfluencerCuts  /  concept B, negative space  /  avatar 800 px", font=font(24), fill=ink)
    x = 60
    for name, c in (("tile", TILE), ("cut piece", TOP), ("body", BODY)):
        d.rectangle([x, 890, x + 56, 946], fill=c, outline=(120, 120, 120))
        d.text((x + 64, 908), f"{name} #{c[0]:02X}{c[1]:02X}{c[2]:02X}", font=font(18), fill=ink)
        x += 265
    for row, (bg, fg) in enumerate(((DARK, (200, 200, 200)), (WHITE, (90, 90, 90)))):
        x0, y0 = 880, 60 + row * 300
        d.rectangle([x0, y0, x0 + 560, y0 + 270], fill=bg)
        cx = x0 + 40
        for dia in (176, 96, 48):
            c = circle_crop(avatar, dia)
            s.paste(c, (cx, y0 + 30 + (176 - dia) // 2), c)
            d.text((cx, y0 + 226), f"{dia} px", font=font(18), fill=fg)
            cx += dia + 56
        # one-colour transparent logo on the same ground
        d.rectangle([1470, y0, 1700, y0 + 270], fill=bg)
        l = logo.resize((200, 200), Image.LANCZOS)
        s.paste(l, (1485, y0 + 35), l)
    d.text((880, 672), "silhouette test (solid black)", font=font(20), fill=ink)
    s.paste(render(240, tile=WHITE, top=(0, 0, 0), body=(0, 0, 0), max_r=0.47).convert("RGB"), (880, 710))
    d.text((1160, 672), "32 px and 24 px", font=font(20), fill=ink)
    cx = 1160
    for dia in (32, 24):
        for bg in (DARK, WHITE):
            d.rectangle([cx - 14, 710, cx + dia + 14, 710 + dia + 28], fill=bg)
            c = circle_crop(avatar, dia)
            s.paste(c, (cx, 724), c)
            cx += dia + 44
    d.text((1160, 800), "contrast: tile on white %.1f, tile on #0F0F0F %.1f" % (contrast(TILE, WHITE), contrast(TILE, DARK)), font=font(18), fill=ink)
    d.text((1160, 830), "cut piece on tile %.1f, body on tile %.1f" % (contrast(TOP, TILE), contrast(BODY, TILE)), font=font(18), fill=ink)
    return s


def main():
    avatar = render(800)
    avatar.convert("RGB").save(HERE / "avatar-800.png")
    logo = render(1024, tile=None, top=MONO, body=MONO, max_r=0.48)
    logo.save(HERE / "logo-transparent-1024.png")
    sheet(avatar, logo).save(HERE / "sheet.png")


if __name__ == "__main__":
    main()
