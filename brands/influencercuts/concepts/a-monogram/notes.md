# Concept A: cut-i + C monogram

## Idea

One rounded slab is split by a vertical cut into an "i" and a "C", so the i is the slice taken off the C's back and the two letters share one outline. A single 30-degree diagonal makes every other cut: it severs the i's head, and the same parallelogram is the piece missing from the C's mouth, so the cream head reads as the clip lifted out of the longer block (and as a head on a body, for "creator").

Drawn entirely from shapes in `make.py` (rounded-rectangle distance fields and half-planes, rendered at 4x and reduced with LANCZOS). No font is used in the mark; Avenir Next is used only for the sheet labels.

## Palette

| Role | Hex | Contrast (my own WCAG calculation) |
|------|-----|------------------------------------|
| Tile, burnt orange | `#EE5A1F` | 3.43 on white, 5.58 on #0f0f0f |
| Mark, ink | `#14110F` | 5.48 on the tile |
| Offcut head, cream | `#FFF4E0` | 3.15 on the tile |

The one-colour version (`logo-transparent-1024.png`) is the whole mark in `#EE5A1F` on a transparent ground, because that colour holds on both white and near-black.

## Research findings applied

- Flat fills on an opaque tile that passes 3:1 on white and on near-black (logo-craft F6, F9). I moved from the suggested coral `#FF5A3C` to a more orange `#EE5A1F`: further from YouTube red, and cream on it rises from 2.84 to 3.15.
- One primary, one neutral, one accent; a one-colour version exists (clip-channels 6, 10; logo-craft F12).
- The cut is the negative space and the terminal, not a drawn tool (logo-craft F1, F4). No play triangle, scissors, film strip, speech bubble, pixel lettering, gradient or glitch offset.
- Few solid parts on one unit grid: three pieces, one stroke weight (0.38 of half-height), one gap width used for both cuts (logo-craft F3, F11).
- Gaps are about 48 px on the 800 px master (6%), the low end of the 6 to 8% rule (logo-craft F7), so they stay open at 48 and 32 px.
- Monogram of two characters fused into one silhouette, with the cut tied to the name (clip-channels 5, 8, 11).
- Mark kept inside the circle crop with about 11% clearance; centred between bounding-box centre and centroid (logo-craft F11, Do 7).
- Checked on the sheet at 176, 96, 48 and 32 px on dark and white (clip-channels 12).

## Iteration log

1. Upright "iC" set side by side, flat-bottomed head, round-cornered C. Legible, but it looked like two letters from a bold font; the cut was a detail.
2. Head made the exact parallelogram removed from the C's mouth. Better story, still two separate letters.
3. Tried three interlocks. "Spine" (the i as the C's only back) read as "iD". "Ledge" (C's bottom arm under the i) read as a cursive tail. "Block" (one slab, vertical cut, C keeps a square back) kept "iC" and read as one emblem; chosen.
4. Widened the C counter, changed the tile to `#EE5A1F`, reduced the mark slightly for circle clearance, fixed sheet layout.

## Known weaknesses

- It is still, at heart, a bold "iC". The cut is clear at 96 px and above; at 32 px it is a monogram with a pale dot.
- The C's square back makes it lean toward a "D" at a glance; the mouth is what rescues it.
- Cream on the tile is only 3.15:1, so the head is the first thing to fade at 32 px. The mark does not depend on it (the one-colour version works).
- Orange with black can read as hardware or Halloween; it says "bold media" more than "creators". The head-on-body reading of the i is the only creator cue and it is subtle.
- The link between the head and the C's mouth (same parallelogram) is something you notice only when told.
- Gaps sit at 6%, the bottom of the recommended range.
- "iC" monograms are common. No reverse-image or trademark search has been done, and I did not view any existing logos, so originality is unchecked beyond avoiding the marks named in the research.
- Not tested over a busy video frame or beside real Shorts thumbnails.
