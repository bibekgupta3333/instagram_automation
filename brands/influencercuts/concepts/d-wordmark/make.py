"""InfluencerCuts, concept D (wordmark-led): "one cut, one shift".

Custom geometric lettering drawn from primitives (no font for the mark itself).
A single diagonal cut runs through the lettering; everything above the cut is
slid along it, like a clip moved on a timeline. The avatar is the "IC" monogram
from the same alphabet with the same cut.

Run:  .venv/bin/python make.py     (writes PNGs next to this file)
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SS = 4  # supersampling factor

# Palette ---------------------------------------------------------------
COBALT = (47, 85, 255)    # #2F55FF  tile
CREAM = (255, 244, 224)   # #FFF4E0  lettering
AMBER = (255, 194, 46)    # #FFC22E  the moved clip (caption yellow)
INK = (15, 15, 15)        # #0F0F0F  presentation only
WHITE = (255, 255, 255)

SMALL_FONT = ("/System/Library/Fonts/Avenir Next.ttc", 8)  # Heavy


# Lettering -------------------------------------------------------------
# Unit = cap height H. Stroke = STROKE * H. All rounds are true circles/ellipses.
STROKE = 0.25
W_U = 0.84
W_T = 0.80
W_S = 0.74
TRACK = 0.13     # letter spacing
MOUTH = 0.36     # width of the C's mouth band (it lies on the cut line)


def _line(angle_deg):
    th = math.radians(angle_deg)
    ux, uy = math.cos(th), -math.sin(th)      # along the cut, rising to the right
    return (ux, uy), (-uy, ux)                # direction, normal (pointing down)


def _c(d, x, y, h, angle=0.0, **_):
    """Circular C. Its mouth is a band through the centre, parallel to the
    cut, so both terminals are sliced on the same angle as the stem."""
    s = STROKE * h
    cx, cy = x + h / 2, y + h / 2
    d.ellipse([x, y, x + h, y + h], fill=255)
    d.ellipse([x + s, y + s, x + h - s, y + h - s], fill=0)
    (ux, uy), (nx, ny) = _line(angle)
    m, far = MOUTH * h / 2, h
    d.polygon([(cx - nx * m, cy - ny * m), (cx + ux * far - nx * m, cy + uy * far - ny * m),
               (cx + ux * far + nx * m, cy + uy * far + ny * m), (cx + nx * m, cy + ny * m)], fill=0)
    return h, None


def _u(d, x, y, h, **_):
    s, w = STROKE * h, W_U * h
    d.ellipse([x, y + h - w, x + w, y + h], fill=255)
    d.rectangle([x, y, x + w, y + h - w / 2], fill=255)
    d.ellipse([x + s, y + h - w + s, x + w - s, y + h - s], fill=0)
    d.rectangle([x + s, y - 2, x + w - s, y + h - w / 2], fill=0)
    return w, None


def _t(d, x, y, h, **_):
    s, w = STROKE * h, W_T * h
    d.rectangle([x, y, x + w, y + s], fill=255)
    d.rectangle([x + w / 2 - s / 2, y, x + w / 2 + s / 2, y + h], fill=255)
    return w, None


def _i(d, x, y, h, **_):
    s = STROKE * h
    d.rectangle([x, y, x + s, y + h], fill=255)
    return s, None


def _s(d, x, y, h, **_):
    """Two stacked elliptical rings sharing the middle stroke."""
    s, w = STROKE * h, W_S * h
    ry = (h + s) / 4
    cx = x + w / 2
    cy1, cy2 = y + ry, y + h - ry
    size = d.im.size
    for cy, quad in ((cy1, "lr"), (cy2, "ul")):
        ring = Image.new("L", size, 0)
        rd = ImageDraw.Draw(ring)
        rd.ellipse([x, cy - ry, x + w, cy + ry], fill=255)
        rd.ellipse([x + s, cy - ry + s, x + w - s, cy + ry - s], fill=0)
        # drop one quadrant per ring. Terminals stay flat: slicing them on the
        # cut angle was tried and closed the counters (the S read as an 8).
        if quad == "lr":   # top ring: drop lower-right quadrant
            rd.rectangle([cx + 1, cy, x + w + 2, cy + ry + 2], fill=0)
        else:              # bottom ring: drop upper-left quadrant
            rd.rectangle([x - 2, cy - ry - 2, cx - 1, cy], fill=0)
        d.bitmap((0, 0), ring, fill=255)
    return w, None


GLYPHS = {"C": _c, "U": _u, "T": _t, "S": _s, "I": _i}


def lettering(text, h, track=TRACK, angle=0.0, cut_index=None):
    """Draw `text` at cap height h. Returns (rest mask, cut-glyph mask, cut-glyph box).
    The glyph at `cut_index` is drawn on its own mask so it alone can be cut."""
    pad = int(h * 0.6)
    size = (int(h * (len(text) + 1) + 2 * pad), int(h + 2 * pad))
    rest, cut = Image.new("L", size, 0), Image.new("L", size, 0)
    x, box = float(pad), None
    for i, ch in enumerate(text):
        target = cut if i == cut_index else rest
        w, _ = GLYPHS[ch](ImageDraw.Draw(target), x, pad, h, angle=angle)
        if i == cut_index:
            box = (x, pad, x + w, pad + h)
        x += w + track * h
    return rest, cut, box


def cut_and_shift(mask, angle_deg, gap, dx, point):
    """Cut `mask` along the line through `point` at `angle_deg` and slide the
    upper part horizontally by `dx` (cap line and baseline stay put). The band
    removed is sized so the gap left after the slide is exactly `gap` wide.
    Returns (lower, upper) masks on the same canvas."""
    a = np.asarray(mask, dtype=np.float32)
    yy, xx = np.mgrid[0:a.shape[0], 0:a.shape[1]].astype(np.float32)
    (ux, uy), (nx, ny) = _line(angle_deg)
    g0 = gap + dx * nx                                     # sliding right closes the gap
    dist = (xx - point[0]) * nx + (yy - point[1]) * ny     # >0 below the line
    lower = np.where(dist > g0 / 2, a, 0).astype(np.uint8)
    upper = np.where(dist < -g0 / 2, a, 0).astype(np.uint8)
    up = Image.fromarray(upper)
    up = up.transform(up.size, Image.AFFINE, (1, 0, -dx, 0, 1, 0), resample=Image.BICUBIC)
    return Image.fromarray(lower), up


def union_bbox(*masks: Image.Image):
    boxes = [m.getbbox() for m in masks if m.getbbox()]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def paint(canvas: Image.Image, mask: Image.Image, colour, xy):
    layer = Image.new("RGBA", mask.size, colour + (255,))
    canvas.paste(layer, xy, mask)


# Mark parameters ---------------------------------------------------------
ANGLE = 26.0        # the one cut angle: stem cut and C terminals share it
GAP = 0.13          # cut gap, in cap heights
SLIDE = 0.125       # how far the cut piece slides, in cap heights


def _assemble(text, h, cut_index, at, track, slide):
    """Returns (base, moved): base = untouched letters + the fixed part of the
    cut stem; moved = the piece that slid."""
    rest, cut, box = lettering(text, h, track=track, angle=ANGLE, cut_index=cut_index)
    stem_x = (box[0] + box[2]) / 2
    point = (stem_x, box[1] + at * h)
    lower, upper = cut_and_shift(cut, ANGLE, GAP * h, slide * h, point)
    return rest, lower, upper


def monogram(h):
    """IC: the I is cut and its top slides toward the C. Returns (base, moved)."""
    rest, lower, upper = _assemble("IC", h, 0, 0.50, track=0.25, slide=0.14)
    base = Image.fromarray(np.maximum(np.asarray(rest), np.asarray(lower)))
    box = union_bbox(base, upper)
    return base.crop(box), upper.crop(box)


def wordmark(h):
    """CUTS: the stem of the T is cut below the bar; the lower stem slides."""
    rest, lower, upper = _assemble("CUTS", h, 2, 0.60, track=TRACK, slide=-SLIDE)
    # here the bar (upper) stays aligned and the lower stem is the moved piece:
    # undo the slide on the upper part by shifting both back
    dx = -SLIDE * h
    back = lambda m: m.transform(m.size, Image.AFFINE, (1, 0, dx, 0, 1, 0), resample=Image.BICUBIC)
    upper, lower = back(upper), back(lower)
    base = Image.fromarray(np.maximum(np.asarray(rest), np.asarray(upper)))
    box = union_bbox(base, lower)
    return base.crop(box), lower.crop(box)


def avatar(size: int = 800) -> Image.Image:
    S = size * SS
    img = Image.new("RGBA", (S, S), COBALT + (255,))
    base, moved = monogram(0.47 * S)
    w, h = base.size
    # optical centring: nudge left/down a touch because the shifted top adds weight top-right
    x, y = (S - w) // 2 - int(0.004 * S), (S - h) // 2 + int(0.004 * S)
    paint(img, base, CREAM, (x, y))
    paint(img, moved, AMBER, (x, y))
    return img.resize((size, size), Image.LANCZOS)


def small_caps(text: str, px: float, tracking: float, colour) -> Image.Image:
    font = ImageFont.truetype(SMALL_FONT[0], int(px), index=SMALL_FONT[1])
    widths = [font.getlength(c) for c in text]
    total = int(sum(widths) + tracking * px * (len(text) - 1)) + 4
    im = Image.new("RGBA", (total, int(px * 1.4)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x = 0.0
    for c, wd in zip(text, widths):
        d.text((x, 0), c, font=font, fill=colour + (255,))
        x += wd + tracking * px
    return im.crop(im.getbbox())


def lockup(width: int, lower_col, upper_col, small_col) -> Image.Image:
    """INFLUENCER (small, tracked) over custom CUTS. Transparent RGBA."""
    W = width * SS
    lo, up = wordmark(100)
    h = 100 * W / lo.size[0]
    lo, up = wordmark(h)
    small = small_caps("INFLUENCER", h * 0.25, 0.0, small_col)
    # track the small line out to the wordmark's lower-left width
    target = int(lo.size[0] * 0.985)
    n = len("INFLUENCER") - 1
    px = h * 0.25
    extra = (target - small.size[0]) / (n * px)
    small = small_caps("INFLUENCER", px, max(extra, 0), small_col)
    gap = int(h * 0.24)
    H = small.size[1] + gap + lo.size[1]
    im = Image.new("RGBA", (max(lo.size[0], small.size[0]), H), (0, 0, 0, 0))
    im.alpha_composite(small, (0, 0))
    paint(im, lo, lower_col, (0, small.size[1] + gap))
    paint(im, up, upper_col, (0, small.size[1] + gap))
    im = im.crop(im.getbbox())
    return im.resize((im.size[0] // SS, im.size[1] // SS), Image.LANCZOS)


def circle_crop(img: Image.Image, d: int) -> Image.Image:
    big = img.resize((d * SS, d * SS), Image.LANCZOS)
    m = Image.new("L", big.size, 0)
    ImageDraw.Draw(m).ellipse([0, 0, big.size[0] - 1, big.size[1] - 1], fill=255)
    big.putalpha(m)
    return big.resize((d, d), Image.LANCZOS)


def transparent_logo(size: int = 1024) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    lk = lockup(int(size * 0.84), COBALT, COBALT, COBALT)
    im.alpha_composite(lk, ((size - lk.size[0]) // 2, (size - lk.size[1]) // 2))
    return im


def sheet(av: Image.Image) -> Image.Image:
    W, H = 1800, 1320
    sh = Image.new("RGBA", (W, H), (244, 241, 234, 255))
    d = ImageDraw.Draw(sh)
    label = ImageFont.truetype("/System/Library/Fonts/Avenir Next.ttc", 22, index=2)
    grey = (110, 110, 110)
    # large avatar (square master) and its circle crop outline
    big = av.resize((640, 640), Image.LANCZOS)
    sh.paste(big, (60, 60))
    d.ellipse([60, 60, 699, 699], outline=(255, 255, 255, 90), width=2)
    d.text((60, 712), "avatar-800.png (square master, circle = YouTube crop)", font=label, fill=grey)
    # circle crops on dark and white
    for row, (bg, name) in enumerate(((INK, "#0f0f0f"), (WHITE, "#ffffff"))):
        x0, y0 = 760, 60 + row * 330
        d.rectangle([x0, y0, x0 + 980, y0 + 300], fill=bg)
        cx = x0 + 70
        for dia in (176, 96, 48):
            c = circle_crop(av, dia)
            sh.alpha_composite(c, (cx, y0 + 150 - dia // 2))
            d.text((cx + dia // 2 - 14, y0 + 262), str(dia), font=label,
                   fill=(150, 150, 150))
            cx += dia + 90
        # fake Shorts row: 48 px avatar next to a channel name
        c = circle_crop(av, 48)
        sh.alpha_composite(c, (x0 + 640, y0 + 126))
        d.text((x0 + 700, y0 + 136), "InfluencerCuts", font=label,
               fill=WHITE if bg == INK else INK)
    # lockups
    y1 = 780
    d.rectangle([60, y1, 60 + 820, y1 + 480], fill=COBALT)
    lk = lockup(600, CREAM, AMBER, CREAM)
    sh.alpha_composite(lk, (60 + (820 - lk.size[0]) // 2, y1 + (480 - lk.size[1]) // 2))
    d.rectangle([920, y1, 920 + 820, y1 + 480], fill=WHITE)
    lk = lockup(600, COBALT, COBALT, COBALT)
    sh.alpha_composite(lk, (920 + (820 - lk.size[0]) // 2, y1 + (480 - lk.size[1]) // 2))
    # one-colour silhouette test + watermark
    lo, up = monogram(64 * SS)
    sil = Image.new("RGBA", lo.size, (0, 0, 0, 0))
    paint(sil, lo, INK, (0, 0)); paint(sil, up, INK, (0, 0))
    sil = sil.resize((sil.size[0] // SS, sil.size[1] // SS), Image.LANCZOS)
    sh.alpha_composite(sil, (1740 - sil.size[0] - 24, y1 + 480 - sil.size[1] - 20))
    d.text((60, y1 + 490), "lockup on tile", font=label, fill=grey)
    d.text((920, y1 + 490), "one-colour lockup (logo-transparent-1024.png) + silhouette test", font=label, fill=grey)
    return sh.convert("RGB")


def _lum(c):
    f = lambda v: v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (f(v / 255) for v in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def main():
    for name, a, b in (("tile on white", COBALT, WHITE), ("tile on #0f0f0f", COBALT, INK),
                       ("cream on tile", CREAM, COBALT), ("amber on tile", AMBER, COBALT)):
        print(f"contrast {name}: {contrast(a, b):.2f}:1")
    av = avatar(800)
    av.convert("RGB").save(HERE / "avatar-800.png")
    transparent_logo(1024).save(HERE / "logo-transparent-1024.png")
    sheet(av).save(HERE / "sheet.png")
    print("wrote avatar-800.png, logo-transparent-1024.png, sheet.png")


if __name__ == "__main__":
    main()
