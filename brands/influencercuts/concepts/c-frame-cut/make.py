"""InfluencerCuts concept C, "frame cut".

An upright video frame (the I) stands beside a trim bracket (the C). One wide diagonal
cut severs the frame and the offcut slides away along the cut, in the accent colour.

Run:  .venv/bin/python make.py
Writes avatar-800.png, logo-transparent-1024.png, sheet.png next to this file.
All geometry is in 800-unit canvas coordinates, drawn at 4x and reduced with LANCZOS.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
N = 800          # master canvas
SS = 4           # supersampling factor

# Palette
TILE = (48, 84, 240)      # cobalt  #3054F0
INK = (255, 246, 229)     # cream   #FFF6E5
ACCENT = (255, 184, 28)   # amber   #FFB81C
DARK = (15, 15, 15)       # sheet only
WHITE = (255, 255, 255)   # sheet only

# Geometry (800-unit canvas). Tuned by eye over several rounds, see notes.md.
P = dict(
    stroke=112,           # bracket stroke
    fr_x0=150, fr_x1=336,  # frame slab (the I) left, right
    y0=170, y1=630,       # shared top and bottom of frame and bracket
    br_x0=422, br_x1=696,  # bracket (the C) outer left, arm ends
    cut_y=404,            # height of the cut centre line at the frame centre
    cut_deg=30,           # cut angle (positive falls to the right)
    gap=66,               # cut width, perpendicular
    slide=-84,            # offcut travel along the cut (negative = up and left)
    radius=12,            # corner radius
    scale=0.89,           # whole mark, about the canvas centre
    shift=(8, 0),       # optical centring shift for the whole mark
)


def grid():
    n = N * SS
    ys, xs = np.mgrid[0:n, 0:n].astype(np.float32)
    return (xs + 0.5) / SS, (ys + 0.5) / SS


def rbox(x, y, x0, y0, x1, y1, r):
    """Filled rounded rectangle as a boolean mask."""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - r, (y1 - y0) / 2 - r
    dx = np.maximum(np.abs(x - cx) - hx, 0)
    dy = np.maximum(np.abs(y - cy) - hy, 0)
    return np.hypot(dx, dy) <= r


def build_masks(p=P):
    """Return (ink_mask, accent_mask) as boolean arrays at N*SS."""
    x, y = grid()
    sx, sy = p["shift"]
    x = (x - N / 2 - sx) / p["scale"] + N / 2
    y = (y - N / 2 - sy) / p["scale"] + N / 2
    r, s = p["radius"], p["stroke"]
    y0, y1 = p["y0"], p["y1"]

    # Frame slab and the cut line through it.
    fcx = (p["fr_x0"] + p["fr_x1"]) / 2
    a = np.deg2rad(p["cut_deg"])
    ux, uy = np.cos(a), np.sin(a)          # along the cut
    nx, ny = -uy, ux                       # normal, pointing down the canvas

    def dist(px, py, cx, cy):
        return (px - cx) * nx + (py - cy) * ny

    fy0 = y0 - uy * p["slide"]   # slab top sits lower by the offcut's rise, so the slid offcut ends level with the C
    frame = rbox(x, y, p["fr_x0"], fy0, p["fr_x1"], y1, r)
    lower = frame & (dist(x, y, fcx, p["cut_y"]) >= p["gap"] / 2)

    # Offcut: the upper part, moved along the cut direction.
    xo, yo = x - ux * p["slide"], y - uy * p["slide"]
    upper = rbox(xo, yo, p["fr_x0"], fy0, p["fr_x1"], y1, r) & (dist(xo, yo, fcx, p["cut_y"]) <= -p["gap"] / 2)

    # Trim bracket: a square C, open on the right, with plain square arm ends
    # (slanted ends were tried and made slivers that vanish at 48 px).
    outer = rbox(x, y, p["br_x0"], y0, p["br_x1"], y1, r)
    inner = (x >= p["br_x0"] + s) & (y >= y0 + s) & (y <= y1 - s)
    bracket = outer & ~inner

    return bracket | lower, upper


def to_img(mask):
    return Image.fromarray((mask * 255).astype(np.uint8), "L").resize((N, N), Image.LANCZOS)


def render_avatar(ink_a, acc_a, tile=TILE, ink=INK, accent=ACCENT):
    im = Image.new("RGB", (N, N), tile)
    im.paste(Image.new("RGB", (N, N), ink), (0, 0), ink_a)
    im.paste(Image.new("RGB", (N, N), accent), (0, 0), acc_a)
    return im


def circle_crop(im, size):
    """Circular crop on transparent, antialiased."""
    big = size * 4
    m = Image.new("L", (big, big), 0)
    ImageDraw.Draw(m).ellipse((0, 0, big - 1, big - 1), fill=255)
    m = m.resize((size, size), Image.LANCZOS)
    out = im.convert("RGB").resize((size, size), Image.LANCZOS).convert("RGBA")
    out.putalpha(m)
    return out


def font(size):
    for path, idx in (("/System/Library/Fonts/Supplemental/Futura.ttc", 0),
                      ("/System/Library/Fonts/Helvetica.ttc", 0)):
        try:
            return ImageFont.truetype(path, size, index=idx)
        except OSError:
            continue
    return ImageFont.load_default()


def make_sheet(avatar, ink_a, acc_a):
    W, H = 1600, 1000
    sheet = Image.new("RGB", (W, H), (236, 234, 229))
    d = ImageDraw.Draw(sheet)
    f_small, f_head = font(22), font(30)

    d.text((60, 40), "InfluencerCuts  /  concept C: frame cut", font=f_head, fill=(30, 30, 30))
    sheet.paste(avatar.resize((640, 640), Image.LANCZOS), (60, 110))
    # Circle guide next to the square, to show the crop.
    d.text((60, 765), "avatar-800.png (square master)", font=f_small, fill=(90, 90, 90))

    # One-colour silhouette test.
    sil = Image.new("RGB", (N, N), WHITE)
    both = Image.fromarray(np.maximum(np.asarray(ink_a), np.asarray(acc_a)))
    sil.paste(Image.new("RGB", (N, N), (0, 0, 0)), (0, 0), both)
    for i, s in enumerate((150, 48, 24)):
        t = sil.resize((s, s), Image.LANCZOS)
        sheet.paste(t, (60 + (0, 170, 240)[i], 810))
    d.text((340, 830), "one-colour silhouette\n150 / 48 / 24 px", font=f_small, fill=(90, 90, 90))

    for row, (bg, fg, label) in enumerate(((DARK, (200, 200, 200), "on #0f0f0f"),
                                           (WHITE, (90, 90, 90), "on white"))):
        x0, y0, x1, y1 = 760, 110 + row * 430, 1540, 500 + row * 430
        d.rectangle((x0, y0, x1, y1), fill=bg)
        d.text((x0 + 24, y0 + 18), label, font=f_small, fill=fg)
        cx = x0 + 70
        for s in (176, 96, 48):
            c = circle_crop(avatar, s)
            cy = (y0 + y1) // 2 + 10 - s // 2
            sheet.paste(c, (cx, cy), c)
            d.text((cx + s // 2 - 22, y1 - 44), f"{s} px", font=f_small, fill=fg)
            cx += s + 110
    return sheet


def main():
    ink_m, acc_m = build_masks()
    ink_a, acc_a = to_img(ink_m), to_img(acc_m)

    avatar = render_avatar(ink_a, acc_a)
    avatar.save(HERE / "avatar-800.png")

    # Transparent logo: the round badge, so the cream mark never sits on a bare background.
    badge = circle_crop(avatar.resize((1024, 1024), Image.LANCZOS), 1024)
    badge.save(HERE / "logo-transparent-1024.png")

    make_sheet(avatar, ink_a, acc_a).save(HERE / "sheet.png")

    # Report: extent of the mark relative to the circular crop.
    ys, xs = np.nonzero(ink_m | acc_m)
    rmax = np.hypot(xs / SS - N / 2, ys / SS - N / 2).max()
    print(f"bbox x {xs.min()/SS:.0f}-{xs.max()/SS:.0f}  y {ys.min()/SS:.0f}-{ys.max()/SS:.0f}  "
          f"centre ({(xs.min()+xs.max())/2/SS:.0f},{(ys.min()+ys.max())/2/SS:.0f})  "
          f"centroid ({xs.mean()/SS:.0f},{ys.mean()/SS:.0f})  max radius {rmax:.0f} of 400")


if __name__ == "__main__":
    main()
