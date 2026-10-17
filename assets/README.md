# assets/

Optional media for the render stage (`studio/stages/render.py`) and the captions stage
(`studio/stages/captions.py`). The repo ships **no media files**: you download them by hand
from a licence-clean source and drop them here. `music/`, `sfx/`, `fonts/` and all `*.mp3` /
`*.wav` files are git-ignored, so nothing here is ever committed or redistributed.

Without any files here a video renders with compressed, loudness-normalised voice only
and a system caption font. Unreadable or silent music and effects are skipped.

```
assets/
  music/<mood>/*.mp3|m4a|wav|ogg|flac   background beds, one folder per mood
  sfx/*.mp3|wav                         short whooshes, played on beat-to-beat transitions
  fonts/*.ttf|otf                       caption/overlay font (first one wins)
```

## Music

Moods: `cinematic`, `ambient`, `documentary`, `corporate`, `upbeat`, `tech`. Each brand has
an ordered mood list in `render.BRAND_MOODS` (gravity-what-if: cinematic, then ambient), and
the first mood folder that has a track wins. The track is chosen per job from a hash of the
job id, so a re-render picks the same one.

What the render does with it: it loops or trims the track to the narration, adds a 0.4 s fade
in and a 1.5 s fade out, and sets it 12 dB under the (compressed) voice. A sidechain
compressor keyed by the voice ducks it further while the voice speaks, to about -20 dB under
speech (measured; see `render.DUCK`). Then the whole mix goes through the two-pass loudnorm to
-14 LUFS. Pick beds without vocals and without a loud drop. Steady instrumental tracks work best.

Overrides: `STUDIO_MUSIC=0` renders voice only. `STUDIO_MUSIC_MOOD=tech` (or `tech,upbeat`)
replaces the brand's moods. `STUDIO_SFX=0` disables whooshes. `STUDIO_ASSETS_DIR=/path` uses
another assets folder.

## Licence rules (read before you drop a file in)

We publish monetised videos to YouTube, Instagram and Facebook, so every track and effect must
be cleared for **commercial use on all three platforms**. Rules:

1. **Only use sources whose licence you have read for that specific file.** Keep a note next to
   each file, with the same name plus `.txt` (`music/cinematic/deep_orbit.mp3` ->
   `deep_orbit.txt`). It should hold the source URL, the author, the licence name, the download
   date and any attribution line it requires. The render does not read these notes. They're
   your proof if a claim comes in.
2. **YouTube Audio Library** (Studio -> Audio Library): use only tracks whose licence type is
   *"You're free to use this song in any of your videos"*, i.e. no attribution required. Tracks
   marked *"Attribution required"* need the exact credit line in the video description, and the
   pipeline does **not** add it automatically. Skip those, or add the credit by hand before you
   approve the video. Check the Audio Library terms before you use a track **outside YouTube**
   (IG/FB Reels). Off-YouTube use is `unverified` for us. When in doubt, keep that track for
   YouTube-only renders.
3. **Pixabay Music / Pixabay sound effects** (Pixabay Content License): commercial use is
   allowed and no attribution is required. You may not redistribute the file standalone, and
   this folder is git-ignored for that reason. Some Pixabay tracks are registered with Content
   ID by their authors, which can trigger claims. Prefer tracks whose page does not mention
   Content ID, and keep the licence certificate/URL in the `.txt` note.
4. **Never use** commercial songs, "no copyright music" YouTube re-uploads, TikTok/IG sounds,
   film or game soundtracks, or anything ripped from a stream. Don't use tracks with an
   NC (non-commercial) or ND clause either.
5. CC-BY tracks are allowed only if you put the attribution in the description yourself, as
   in rule 2. CC0/public-domain tracks are fine.

## SFX

Short (0.3 to 0.8 s) whooshes or swishes. They play about 0.18 s before each beat-to-beat
transition, about 9 dB under the voice. With several files they rotate. The same licence rules
apply (Pixabay sound effects are the easy source).

## Fonts

Captions default to the macOS system face **Arial Black**, falling back to Helvetica Neue,
then Impact. To use your own bold face, drop one `.ttf`/`.otf` here. Free display faces under
the SIL Open Font License work well: Anton, Archivo Black, Bebas Neue, Montserrat ExtraBold and
Poppins Black. The captions stage reads the font's family name (fontTools), and the render
passes this folder to libass as `fontsdir`. `STUDIO_CAPTION_FONT="Family Name"` overrides the
font and can also name any installed system font. Keep the font's `OFL.txt` next to it.
