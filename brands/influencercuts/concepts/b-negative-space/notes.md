# Concept B: negative space, "The Cut Creator"

## The idea

One solid disc with a head and shoulders cut out of it, so the empty space is the creator. A single diagonal slice passes through the neck and the two halves slide apart along the cut, like two clips either side of an edit point; the cut-off piece is amber and the rest is cream.

There are no letters, no play triangle, no speech bubble and no rounded-square badge.

## Palette

| Role | Hex | Note |
|------|-----|------|
| Tile | `#121740` ink navy | Avatar background |
| Cut piece | `#FFC82E` caption amber | Upper half; 11.1:1 on the tile |
| Body | `#FFF6E2` cream | Lower half; 16.0:1 on the tile |
| One-colour logo | `#FF7A1A` orange | Only in `logo-transparent-1024.png`, chosen to read on both white and dark |

Contrast figures are my own WCAG calculation in `make.py`, not measured on a device.

## Files

- `make.py`: builds everything. Run with the project `.venv` Python.
- `avatar-800.png`: 800x800, opaque. The farthest mark pixel is 0.43 of the canvas from the centre, so a circular crop (0.50) leaves a margin.
- `logo-transparent-1024.png`: RGBA, one colour.
- `sheet.png`: avatar, circular crops at 176, 96, 48, 32 and 24 px on `#0F0F0F` and white, a solid-black silhouette test and the one-colour logo.

## Research findings applied

- Design for a 40 to 52 px circle first: three filled shapes, no text, no outlines (clip-channels: Do; logo-craft: avatar finding).
- No gap under about 6 to 8 % of canvas width: the slice is about 7 % of the avatar width, and the solid strip between the head and the cut is about 5 % (see weaknesses).
- Negative space used as the focal point, slanted blocks and a deep cut that implies motion (logo-craft, LogoLounge trends).
- Flat colour, no gradient (both briefs).
- Fuse two ideas in one silhouette: creator plus cut, with the cut readable in the outline (clip-channels: Do).
- Solid-black silhouette test: on the sheet, and it holds.
- One primary, one neutral, one accent, plus a one-colour version (clip-channels: Do).
- Palette follows the clip-channels suggestion of caption amber on ink navy, and stays clear of YouTube red, Twitch purple, Kick neon green and TikTok cyan and pink.
- Avoided: play triangles, speech bubbles, scissors, film strips, pixel letters, glitch offsets, faces or likenesses of real people, plain initials.

## Rounds

1. Upright oval "C" with a clip-frame hole, a slab "i" inside and a slice through both. Rejected: it read as a "6", "G" or "B" and as a coin at 48 px.
2. Three alternatives side by side (ring with phone slab, sliced person, "C" with a spark). The ring read as a tick box and the spark as an AI or magic-wand icon. The sliced person was the only one that said both "creator" and "cut" at 48 px, so I rebuilt it as a disc with the person cut away.
3. Tested the slice through the head against through the neck (the neck keeps the head whole and legible), and six palettes. Moved from cobalt to ink navy, widened the slide so the outline is clearly two displaced pieces, enlarged the head and re-centred on the bounding circle.

## Known weaknesses

- **The tile fails one research rule.** Ink navy is 17.2:1 on white but only 1.1:1 on `#0F0F0F`, so the tile edge disappears in YouTube's dark theme. The mark itself stays clear. A cobalt tile (`#2447F5`, 6.4:1 and 3.0:1) passes the rule; I tried it and found it looked toy-like with amber and cream.
- **It is built on the generic profile glyph.** At 24 to 32 px it can pass for a default "no picture" avatar, and a slash across a person icon can read as "user blocked".
- **Other readings at small sizes:** a person in a yellow hard hat, a ball with a band, or a yin-yang-like split.
- **It does not say "Shorts" or "video".** It says "a person, cut"; nothing in it is specific to clips.
- **The strip between the head and the cut is about 5 % of canvas width**, under the 6 to 8 % guideline. It holds at 48 px on the sheet and starts to close at 24 px.
- **No visual or trademark check.** The research viewed no logo images and I have not run a reverse-image or trademark search. Profile-glyph logos are common, so this needs a check by eye against Medal, Opus Clip, Outplayed, Streamlabs and large clip channels before use.
- Not tested beside real Shorts first frames at phone size (a research "Do").
