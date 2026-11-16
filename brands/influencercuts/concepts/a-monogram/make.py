"""InfluencerCuts concept A: geometric "iC" monogram drawn from shapes (no font in the mark).

Run:  python make.py            -> avatar-800.png, logo-transparent-1024.png, sheet.png
      python make.py --explore  -> explore.png (variant contact sheet, working aid)

The mark is built as boolean masks at 4x and downscaled with LANCZOS.
Mark units: the letter height is 2.0 (y from -1 to +1, y up).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SS = 4  # supersampling factor


def hexrgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


@dataclass(frozen=True)
class Palette:
    tile: str = "#EE5A1F"
    ink: str = "#14110F"
    accent: str = "#FFF4E0"


@dataclass(frozen=True)
class Spec:
    s: float = 0.38        # stroke weight
    g: float = 0.21        # cut-gap, measured perpendicular to the cut
    slope_deg: float = 30  # the one diagonal used by every cut
    c_w: float = 1.48      # C width
    c_r: float = 0.56      # C outer corner radius
    over: float = 0.06     # how far the head's high corner overshoots the cap line
    slide: float = 0.0   # head displacement along the cut (negative = down-left)
    mouth: float = 0.58    # mouth opening, vertical; the head is the same parallelogram
    mouth_y: float = 0.06  # mouth centre (at the middle of the right stroke)
    ic_gap: float = 0.21   # vertical channel between I and C
    r_left: float = 0.26   # block mode: radius of the shared outer-left corners
    mode: str = "block"    # beside | spine | ledge (structural variants tried in exploration)
    fill: float = 0.39    # max radius of the mark as a fraction of canvas width


def rrect(x, y, cx, cy, hx, hy, r):
    qx = np.abs(x - cx) - (hx - r)
    qy = np.abs(y - cy) - (hy - r)
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r


def masks(spec: Spec, x, y):
    """Return (body, head) boolean masks in mark units."""
    t = np.tan(np.radians(spec.slope_deg))
    ca, sa = np.cos(np.radians(spec.slope_deg)), np.sin(np.radians(spec.slope_deg))
    s = spec.s
    # --- I: stem and severed head -------------------------------------
    ix0, ix1 = 0.0, s
    gv = spec.g / ca                      # vertical thickness of the cut
    # cut line (upper edge = head underside): passes (ix1, 1 - head_r)
    def v(xx, yy):
        return yy - t * (xx - ix1)
    # the head is the exact parallelogram removed from the C's right stroke
    k_hi = 1 + spec.over
    k_top = k_hi - spec.mouth
    in_bar = (x >= ix0) & (x <= ix1) & (y >= -1)
    stem = in_bar & (v(x, y) <= k_top - gv)
    dx, dy = spec.slide * ca, spec.slide * sa
    xs, ys = x - dx, y - dy
    head = (xs >= ix0) & (xs <= ix1) & (v(xs, ys) >= k_top) & (v(xs, ys) <= k_hi)
    # --- C: rounded-rectangle ring with a slanted mouth -----------------
    if spec.mode == "spine":
        # the I is the C's back: one ring, split by a vertical channel
        cx0 = 0.0
        w = spec.c_w + s + spec.ic_gap
        ccx = cx0 + w / 2
        outer = rrect(x, y, ccx, 0, w / 2, 1.0, spec.c_r) <= 0
        inner = rrect(x, y, ccx, 0, w / 2 - s, 1.0 - s, max(spec.c_r - s, 0.06)) <= 0
        xr = cx0 + w - s / 2
        vm = y - t * (x - xr) - spec.mouth_y
        mouth = (x > ccx) & (np.abs(vm) < spec.mouth / 2)
        c = outer & ~inner & ~mouth & (x >= ix1 + spec.ic_gap)
        stem = stem & outer
        return stem | c, head
    cx0 = ix1 + spec.ic_gap
    ccx = cx0 + spec.c_w / 2
    if spec.mode == "block":
        # one rounded block, split by a vertical channel: I is the slice taken off the C's back
        right = cx0 + spec.c_w
        env = rrect(x, y, right / 2, 0, right / 2, 1.0, max(spec.r_left, 0.001)) <= 0
        big = rrect(x, y, right - 3, 0, 3, 1.0, spec.c_r) <= 0
        inner = rrect(x, y, ccx, 0, spec.c_w / 2 - s, 1.0 - s, max(spec.c_r - s, 0.06)) <= 0
        xr = right - s / 2
        vm = y - t * (x - xr) - spec.mouth_y
        mouth = (x > ccx) & (np.abs(vm) < spec.mouth / 2)
        c = env & big & (x >= cx0) & ~inner & ~mouth
        return (stem & env) | c, head
    outer = rrect(x, y, ccx, 0, spec.c_w / 2, 1.0, spec.c_r) <= 0
    inner = rrect(x, y, ccx, 0, spec.c_w / 2 - s, 1.0 - s, max(spec.c_r - s, 0.06)) <= 0
    xr = cx0 + spec.c_w - s / 2           # middle of the right stroke
    vm = y - t * (x - xr) - spec.mouth_y
    mouth = (x > ccx) & (np.abs(vm) < spec.mouth / 2)
    c = outer & ~inner & ~mouth
    if spec.mode == "ledge":
        # the C's bottom arm runs left under the I, which stands on it
        ledge = rrect(x, y, (ix0 + ccx) / 2, -1 + s / 2, (ccx - ix0) / 2, s / 2, 0.16) <= 0
        c = c | ledge
        stem = stem & (y >= -1 + s + spec.g)
    return stem | c, head


def layout(spec: Spec):
    """Find the optical centre and scale: returns (cx, cy, max_radius) in mark units."""
    n = 700
    lin = np.linspace(-1.6, 3.2, n)
    x, y = np.meshgrid(lin, lin[::-1])
    body, head = masks(spec, x, y)
    m = body | head
    xs, ys = x[m], y[m]
    bx, by = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    gx, gy = xs.mean(), ys.mean()
    cx, cy = 0.5 * bx + 0.5 * gx, 0.5 * by + 0.5 * gy   # between box centre and centroid
    rad = np.hypot(xs - cx, ys - cy).max()
    return cx, cy, rad


def render_masks(spec: Spec, size: int):
    n = size * SS
    cx, cy, rad = layout(spec)
    scale = spec.fill * n / rad
    px = (np.arange(n) + 0.5)
    X, Y = np.meshgrid(px, px)
    x = (X - n / 2) / scale + cx
    y = -(Y - n / 2) / scale + cy
    body, head = masks(spec, x, y)
    to_img = lambda m: Image.fromarray((m * 255).astype(np.uint8), "L").resize((size, size), Image.LANCZOS)
    return to_img(body), to_img(head)


def avatar(spec: Spec, pal: Palette, size: int = 800) -> Image.Image:
    body, head = render_masks(spec, size)
    im = Image.new("RGB", (size, size), hexrgb(pal.tile))
    im.paste(Image.new("RGB", (size, size), hexrgb(pal.ink)), (0, 0), body)
    im.paste(Image.new("RGB", (size, size), hexrgb(pal.accent)), (0, 0), head)
    return im


def transparent(spec: Spec, colour: str, size: int = 1024) -> Image.Image:
    """One-colour mark on a transparent ground (watermark / lockup use)."""
    body, head = render_masks(replace(spec, fill=0.46), size)
    alpha = Image.fromarray(np.maximum(np.asarray(body), np.asarray(head)), "L")
    im = Image.new("RGBA", (size, size), hexrgb(colour) + (0,))
    im.putalpha(alpha)
    return im


def circle_crop(av: Image.Image, d: int) -> Image.Image:
    small = av.resize((d, d), Image.LANCZOS).convert("RGBA")
    m = Image.new("L", (d * SS, d * SS), 0)
    ImageDraw.Draw(m).ellipse((0, 0, d * SS - 1, d * SS - 1), fill=255)
    small.putalpha(m.resize((d, d), Image.LANCZOS))
    return small


def font(size: int):
    for p in ("/System/Library/Fonts/Avenir Next.ttc", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def sheet(av: Image.Image, mono: Image.Image, pal: Palette) -> Image.Image:
    W, H = 1800, 1000
    sh = Image.new("RGB", (W, H), "#E9E6E1")
    d = ImageDraw.Draw(sh)
    f, fs = font(26), font(20)
    d.text((60, 40), "InfluencerCuts  /  concept A  /  cut-i + C monogram", font=f, fill="#14110F")
    sh.paste(av.resize((760, 760), Image.LANCZOS), (60, 110))
    d.text((60, 884), "avatar-800.png (square upload)", font=fs, fill="#5a5650")
    # palette chips
    x = 60
    for name, hx in (("tile", pal.tile), ("ink", pal.ink), ("accent", pal.accent)):
        d.rectangle((x, 930, x + 36, 966), fill=hx, outline="#14110F")
        d.text((x + 46, 936), f"{name} {hx}", font=fs, fill="#14110F")
        x += 250
    # circular crops on dark and white
    px0 = 880
    for row, (bg, fg, label) in enumerate((("#0f0f0f", "#f1f1f1", "dark #0f0f0f"), ("#ffffff", "#0f0f0f", "white"))):
        y0 = 110 + row * 300
        d.rectangle((px0, y0, W - 60, y0 + 270), fill=bg)
        d.text((px0 + 20, y0 + 14), label, font=fs, fill=fg)
        cx = px0 + 60
        for dia in (176, 96, 48):
            c = circle_crop(av, dia)
            sh.paste(c, (cx, y0 + 150 - dia // 2), c)
            d.text((cx + dia // 2 - 14, y0 + 242), str(dia), font=fs, fill=fg)
            cx += dia + 70
        # comment-row mock at 48 px
        mx = cx + 10
        for i in range(2):
            yy = y0 + 70 + i * 90
            c = circle_crop(av, 48 if i == 0 else 32)
            sh.paste(c, (mx, yy), c)
            d.rectangle((mx + 64, yy + 6, mx + 250, yy + 16), fill=fg)
            d.rectangle((mx + 64, yy + 28, mx + 190, yy + 36), fill="#8a8a8a")
        d.text((mx, y0 + 242), "in a feed row: 48, 32", font=fs, fill=fg)
    # one-colour versions
    y0 = 710
    half = (W - 60 - px0 - 20) // 2
    for i, (bg, colour, label) in enumerate((("#0f0f0f", pal.tile, "one colour"), ("#ffffff", pal.tile, "one colour"))):
        x0 = px0 + i * (half + 20)
        d.rectangle((x0, y0, x0 + half, y0 + 250), fill=bg)
        m = Image.new("RGBA", mono.size, hexrgb(colour) + (0,))
        m.putalpha(mono.getchannel("A"))
        big = m.resize((220, 220), Image.LANCZOS)
        sh.paste(big, (x0 + 30, y0 + 15), big)
        sm = m.resize((56, 56), Image.LANCZOS)
        sh.paste(sm, (x0 + 300, y0 + 110), sm)
        d.text((x0 + 290, y0 + 14), label, font=fs, fill="#f1f1f1" if i == 0 else "#0f0f0f")
    return sh


SPEC = Spec()
PAL = Palette()


def explore():
    variants = [
        ("beside", SPEC, PAL),
        ("block sq", replace(SPEC, mode="block"), PAL),
        ("block r.3", replace(SPEC, mode="block", r_left=0.3), PAL),
        ("block r.56", replace(SPEC, mode="block", r_left=0.56), PAL),
        ("block sq tight", replace(SPEC, mode="block", c_w=1.3, c_r=0.62), PAL),
        ("block heavy", replace(SPEC, mode="block", s=0.40, c_w=1.42, r_left=0.2), PAL),
        ("block cobalt", replace(SPEC, mode="block", r_left=0.3), Palette("#2F5BFF", "#FFF4E0", "#FFB300")),
        ("block teal", replace(SPEC, mode="block", r_left=0.3), Palette("#FF5A3C", "#1B1464", "#FFF4E0")),
    ]
    cell = 400
    out = Image.new("RGB", (cell * 4, (cell + 110) * 2), "#777")
    d = ImageDraw.Draw(out)
    for i, (name, sp, pal) in enumerate(variants):
        av = avatar(sp, pal, 400)
        x0, y0 = (i % 4) * cell, (i // 4) * (cell + 110)
        out.paste(av, (x0, y0))
        d.rectangle((x0, y0 + cell, x0 + cell, y0 + cell + 110), fill="#0f0f0f" if i % 2 == 0 else "#fff")
        for j, dia in enumerate((96, 48, 32)):
            c = circle_crop(av, dia)
            out.paste(c, (x0 + 10 + j * 110, y0 + cell + 55 - dia // 2), c)
        d.text((x0 + 300, y0 + cell + 40), name, font=font(16), fill="#888")
    out.save(HERE / "explore.png")


def main():
    if "--explore" in sys.argv:
        explore()
        return
    av = avatar(SPEC, PAL, 800)
    av.save(HERE / "avatar-800.png")
    mono = transparent(SPEC, PAL.tile, 1024)
    mono.save(HERE / "logo-transparent-1024.png")
    sheet(av, mono, PAL).save(HERE / "sheet.png")


if __name__ == "__main__":
    main()
