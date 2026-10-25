# Long-form Script Template

```text
# SCRIPT — [WORKING TITLE]

FORMAT:
TARGET DURATION:
WPM BASELINE:
VIEWER:
ONE TAKEAWAY:
PRIMARY CTA:

## HOOK

[0:00]
[SPOKEN WORDS]
[VISUAL: first proof or promise-confirming image]

## SETUP

[0:XX]
[SPOKEN WORDS]
[LOOP OPEN L1: viewer question]

## SECTION 1 — [VIEWER QUESTION]

[0:XX]
[SPOKEN WORDS]
[SCREEN: concrete evidence or demonstration]

## SECTION 2 — [VIEWER QUESTION]

[0:XX]
[SPOKEN WORDS]
[B-ROLL: actionable direction]
[LOOP CLOSE L1: payoff]

## FINAL PAYOFF

[0:XX]
[SPOKEN WORDS]

## CTA / WATCH NEXT

[0:XX]
[SPOKEN WORDS]

## OPEN CONFIRMATIONS

- [CREATOR: confirm ...]
```

Add sections only when the argument requires them. Do not force a fixed chapter count.

## Studio JSON mapping

Long-form uses the same `Script` JSON: `hook` (first 5 s), then 40-70 `beats` grouped
into 4-6 chapters of 6-12 beats (chapter headings are planning only, never fields), each
chapter ending on a pull into the next, and a `cta` loop line that calls back to the hook.
At 150 wpm the spoken total is about 1,200-1,500 words (8-10 min). Validate with
`--format long`.
