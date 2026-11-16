# Concept C: frame cut

## Idea

An upright video frame (read as the "I") stands beside a square trim bracket, like the in/out handle on an editing timeline (read as the "C"). One wide diagonal cut severs the frame, and the offcut slides up and left along the cut in amber: the piece that was clipped out.

## Palette

| Role | Hex | Use |
|------|-----|-----|
| Cobalt | `#3054F0` | Tile |
| Cream | `#FFF6E5` | Frame and bracket |
| Amber | `#FFB81C` | The offcut only |

Contrast ratios (my own WCAG calculation in the shell, not checked with an external tool): tile on white 5.75, tile on #0f0f0f 3.33, cream on tile 5.36, amber on tile 3.32.

## Research findings applied

- Flat fills, opaque tile, no gradient, glow or outline (logo-craft Do 1, Avoid 1-2).
- Three solid parts on shared top and bottom lines, one cut angle (logo-craft Do 6; clip-channels "few filled shapes").
- The cut is a gap, and the moved offcut shows that an edit happened (logo-craft Do 5, D1 "Split I"; clip-channels "the cut should be readable in the shape").
- Two ideas in one silhouette: IC monogram plus frame-and-trim-bracket (clip-channels Do, "Trim handles" direction).
- Stroke and gap sizes: bracket stroke about 100 px and cut gap about 59 px on the 800 px master, both above the 6-8 percent floor (logo-craft Do 2). The I-to-C gap is about 77 px.
- Three colours: one dominant, one neutral, one accent (both briefs).
- Tile reaches 3:1 on both white and near-black (logo-craft Do 4). The first cobalt (#2646E8) gave 2.85 on #0f0f0f, so it was lightened.
- Mark stays inside the circle: furthest point is 342 px from centre of a 400 px radius, about 14 percent padding (logo-craft Do 7).
- Cobalt lane chosen to stay clear of YouTube red, Twitch violet, Kick green and TikTok cyan/pink. No play triangle, scissors, film-strip sprockets, speech bubble, pixel lettering, glitch offset or X-like crossing. Square arm ends replaced slanted ones, which made slivers (clip-channels Avoid "sharp slivers").
- One-colour silhouette is on the sheet at 150, 48 and 24 px (logo-craft Do 8-9).
- `logo-transparent-1024.png` is the round badge, not a bare cream mark, because a pale mark on a transparent background fails on white (logo-craft Avoid 10).

## Rounds

1. Bracket wrapped round the frame. A clearance mask ate the bracket, leaving stepped notches; three unrelated shapes. Dropped.
2. Frame left, bracket right, slanted bracket ends. Read as IC, but the slanted ends made thin slivers and a bug split the bracket spine.
3. Square bracket ends. Clean, but the offcut was small and sat above the bracket top line.
4. Frame top lowered so the slid offcut ends level with the bracket; C widened.
5. Cut lowered to enlarge the offcut, mark scaled up, I-to-C gap widened, tile lightened for dark-mode contrast.

## Known weaknesses

- It is closer to a sliced "IC" monogram than to a literal frame or timeline. The frame and trim-bracket reading needs explaining; nobody will get it unprompted.
- Overlaps with the briefs' "Split I" and "Sliced monogram" directions, so it may look similar to another agent's concept.
- In one colour the cut I can read as a lightning bolt or an "N" stroke; the amber is doing real work, which breaks the "must work in one colour" rule in part.
- At 48 px the cut and the IC survive, but the offcut is only about 10 px wide. At 24 px it is a blob plus a C.
- Nothing in it says "creators" or "influencers". It says "cuts" and the initials only.
- Blue with white and yellow is a common pairing (several retail and tech brands). No reverse-image or trademark search was done, and I did not view any competitor logo, so originality is unchecked.
- The mass leans slightly up and left because of the offcut; centring was done by bounding box and by eye on the sheet, not tested in the real YouTube UI or over a video frame.
- No banner lockup or watermark variant was made.
