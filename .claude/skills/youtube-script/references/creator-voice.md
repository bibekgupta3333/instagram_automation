# Creator Voice Encoding

> **Studio adaptation.** Our channels have no creator: each is a faceless brand with an
> AI-generated Kokoro voice. The "voice profile" below is built from the brand pack
> (`references/channel-rules.md`: niche, hook styles, banned phrases, red lines) plus any
> approved past scripts in `output/*/script.json` for that channel. Voice confidence is
> MEDIUM when built from the pack alone. The trust contract always includes: never claim to
> be human, no credentials, no personal anecdotes, no advice. On-camera delivery means TTS
> delivery: 150 wpm times the pack's `voice.speed`.

Load this reference when creator artifacts or a reusable voice profile are available.

## Evidence hierarchy

Prefer, in order:

1. Unscripted speech or transcripts
2. Creator-written scripts, newsletters, and posts
3. Approved published work
4. Creator-stated preferences
5. Abstract tone labels

Do not infer a stable pattern from one isolated sentence.

## Extract

- **Cadence:** average sentence length, fragments, pauses, transitions, emphasis
- **Vocabulary:** recurring technical terms, colloquialisms, banned phrases
- **Explanation pattern:** example-first, mechanism-first, story-first, proof-first
- **Persuasion pattern:** evidence, demonstration, contrast, narrative, authority
- **Humor:** frequency, type, acceptable targets
- **Epistemic style:** certainty, caveats, sourcing, distinction between fact and judgment
- **On-camera behavior:** WPM, pause rhythm, energy arc, recurring visual devices
- **Trust contract:** claims and tactics the creator will not use

## Voice profile

```text
VOICE CONFIDENCE: HIGH | MEDIUM | LOW
SOURCES: [artifacts]

CADENCE
- [pattern + 2 examples]

PHRASES TO USE
- [verbatim phrase + source]

PHRASES TO AVOID
- [phrase + reason]

EXPLANATION DEFAULT
- [pattern]

PROOF DEFAULT
- [pattern]

ON-CAMERA DELIVERY
- WPM:
- Pause rhythm:
- Energy arc:

TRUST CONTRACT
- Promises:
- No-fly zones:
```

## Voice QA

- Read representative lines aloud.
- Compare against at least three source artifacts when available.
- Flag a line if any competent creator in the niche could say it unchanged.
- Add specificity before adding stylistic quirks.
- Never imitate a living creator so closely that authorship could be confused; abstract transferable traits instead.
