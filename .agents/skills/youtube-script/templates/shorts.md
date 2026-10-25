# Shorts Script Template

```text
# SHORT — [WORKING TITLE]

TARGET DURATION:
VIEWER:
ONE TAKEAWAY:
PRIMARY CTA:

## OPEN / PAYOFF
[0:00]
[SPOKEN WORDS]
[VISUAL: visible result or tension]

## MECHANISM
[0:XX]
[SPOKEN WORDS]
[SCREEN: proof, example, or demonstration]

## CLOSE
[0:XX]
[SPOKEN WORDS]

## CTA — OPTIONAL
[0:XX]
[SPOKEN WORDS]

## OPEN CONFIRMATIONS
- [CREATOR: confirm ...]
```

Do not compress a long-form introduction into a Short. Remove it.

## Studio JSON mapping

The pipeline consumes `studio.contracts.Script` JSON, not this Markdown. Plan with the
template above, then map it:

| Template part | Script JSON |
|---|---|
| OPEN / PAYOFF spoken words | `hook` (<= 5 words, first 1-2 s) |
| `[VISUAL: ...]` under the open | `beats[0].visual_prompt` (the frame during the hook) |
| stakes, MECHANISM, payoff | `beats[]` (8-12), one idea and one picture each |
| `[SCREEN: ...]` / `[B-ROLL: ...]` | `visual_prompt` + `shot` + optional `stock_query` |
| on-screen label | `overlay` (1-5 words; never text inside the image) |
| CLOSE / CTA | `cta`, a loop line back into the hook (no subscribe ask) |
| open confirmations | removed before JSON; never spoken |

Skeleton: `templates/short.json`. Worked example: `examples/wing-and-turbine-short.json`.
