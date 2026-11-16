# Concept D: wordmark-led, "one cut, one slide"

## Idea

A custom heavy geometric alphabet (drawn from circles and bars, no font) in which one straight stem is cut on a 26 degree diagonal and the cut piece slides sideways, like a clip moved on a timeline; the C's two terminals are sliced on the same angle. In the wordmark the cut lands on the stem of the T in "CUTS"; in the avatar it lands on the I of the "IC" monogram, and the moved piece is the only amber shape.

## Palette

| Role | Hex | Notes |
|------|-----|-------|
| Tile (cobalt) | `#2F55FF` | 5.43:1 on white, 3.53:1 on `#0F0F0F` (calculated in `make.py`) |
| Lettering (cream) | `#FFF4E0` | 4.99:1 on the tile |
| Moved clip (amber) | `#FFC22E` | 3.37:1 on the tile; chosen to match yellow burned-in captions |

One-colour version: all cobalt (this is `logo-transparent-1024.png`), or all ink for a watermark.

## Files

- `make.py`: regenerates everything (`.venv/bin/python make.py`). Draws at 4x and downscales with LANCZOS.
- `avatar-800.png`: opaque cobalt tile, IC monogram. Furthest mark pixel is 343 px from centre (radius 400), so about 14% circular padding.
- `logo-transparent-1024.png`: RGBA, one-colour "INFLUENCER / CUTS" lockup. "INFLUENCER" is Avenir Next Heavy, tracked; "CUTS" is drawn.
- `sheet.png`: avatar large, circle crops at 176, 96 and 48 px on `#0f0f0f` and white, both lockups, silhouette test.

## Research findings applied

- Flat fills only, opaque tile, three colours (one dominant, one neutral, one accent). logo-craft Do 1, 3; clip-channels Do "one primary, one neutral, one accent".
- Tile reaches 3:1 on both white and near-black; cobalt is one of the two open colour lanes named. logo-craft Do 4.
- Avatar text limited to "IC"; the full name appears only in the banner lockup. clip-channels Do "keep text to IC".
- The cut is a gap in the letterform, so negative space carries the idea. logo-craft Do 5, findings F1 and F4.
- Built from a few geometric parts on one unit (cap height), stroke 0.25 of cap height. logo-craft Do 6.
- Minimum sizes on the 800 px master: stroke 94 px (11.7%), cut gap 49 px (6.1%), C mouth 135 px. logo-craft Do 2.
- One-colour version designed in; silhouette shown on the sheet. logo-craft Do 8, clip-channels silhouette test.
- Family delivered: avatar mark, one-colour version, banner lockup. logo-craft Do 10.
- "Plain initials with no idea attached" avoided: the initials carry the cut. clip-channels Avoid.
- Avoided: play triangle, scissors, film strip, speech bubble, pixel letters, glitch offsets, gradients, red, violet, neon green, cyan with pink, white-on-black crossing strokes, any face or creator name.
- Closest research directions: logo-craft D1 "Split I" and clip-channels 1 "Sliced monogram", but the cut is confined to one stem after testing.

## Iteration log

1. Whole-monogram and whole-word diagonal slice, upper part shifted, two-tone. Rejected: C and U counters closed up, the cut left slivers at the C mouth, "CUTS" was hard to read.
2. Rebuilt letters with true circles and a C mouth lying on the cut line. Rejected: the monogram read as "IO", "10" or "!O" at every size.
3. Cut confined to one stem (I in the avatar, T in the wordmark), slide made horizontal so cap line and baseline hold. Legible; I read as lowercase "i"; tile was 2.95:1 on dark.
4. Cut moved to mid-height, tile lightened to `#2F55FF`, "INFLUENCER" tracked to full width. Tried slicing the S terminals on the cut angle: closed the counters (read as "8"), reverted.
5. Mark reduced from 0.505 to 0.47 of canvas (padding 8% to 14%); cut gap widened from 4.7% to 6.1% of canvas.

## Known weaknesses

- At 48 px the cut I can read as a lowercase "i" or an inverted "!", because the amber top looks like a dot.
- The C's opening is tilted 26 degrees and its lower terminal is longer than the upper; some viewers may see a "G", a refresh arrow or a power symbol. Not tested with people.
- It says "cuts" moderately and "creators" hardly at all. Nothing in the mark refers to streamers or captions except the amber colour.
- The S is the weakest glyph: flat terminals do not share the cut angle, and its middle is heavy.
- Cobalt tile with a pale monogram is a crowded general category (many apps use blue and white). Only the platforms named in the research were considered.
- The cut gap is at the bottom of the 6 to 8% rule (6.1%). At 24 px it is about 1.5 px and will start to close.
- Amber on white is weak (about 1.6:1), so the two-colour version must stay on the cobalt tile; use the one-colour version elsewhere.
- No reverse image search or trademark search has been run. No comparison was made against real clip-channel avatars or over a busy video frame. Sizes below 48 px were not rendered.
- "INFLUENCER" uses a system font (Avenir Next Heavy); check its licence terms before using the lockup commercially outside macOS-rendered artwork.
