# Quality Gates

> **Studio adaptation.** Run `scripts/validate_script.py` on the Script JSON (structural and
> policy gates). Gates 1-7 below remain the semantic review. Extra gates for this studio:
> **Policy** (faceless AI voice, no credentials, no advice, brand red lines and banned
> templates), **Image** (no text, logos, numbers or people in any `visual_prompt`; labels
> only in `overlay`, 1-5 words), **Disclosure** (affiliate or sponsor: spoken before the
> endorsement and in `description`). Gate 6 timestamps map to beat order; gate 7 "CTA" is
> the loop line, and a subscribe ask fails it.

Structural validation is necessary but insufficient. Run all applicable gates.

## 1. Promise gate

- Packaging and hook make the same promise.
- Proof begins early enough to confirm the click.
- The final payoff fulfills the central promise without qualification drift.

## 2. Truth gate

- Every statistic, quote, factual comparison, and product claim maps to the evidence ledger.
- Personal results are labeled creator-provided.
- Opinion is grammatically framed as judgment.
- Open claims retain `[CREATOR: confirm ...]`.

## 3. Voice gate

- Cadence matches artifacts rather than generic “creator voice.”
- No unexplained jargon.
- No dense clusters of hype words or AI-writing tells.
- Distinctive lines come from the creator’s own worldview and proof.
- Read-aloud pass: no buried verbs, breathless sentences, or accidental essay transitions.

## 4. Density gate

For every beat ask:

- Does it advance the argument, proof, emotion, or viewer orientation?
- Does it earn the next line?
- Can 30% be removed without reducing meaning?

Cut repeated promises, repeated summaries, throat-clearing, redundant examples, and framework labels spoken aloud.

## 5. Retention gate

- Central loop opens and closes.
- Re-engagement beats change information mode.
- Dense passages receive examples, visuals, or pauses.
- Suspense is earned and payoff timing is explicit.
- Risk statements are hypotheses, not fake retention predictions.

## 6. Production gate

- Spoken text is visually distinct from directions.
- Important claims have feasible proof visuals.
- Timestamps are monotonic.
- Runtime and word count are compatible.
- Editor notes are actionable and do not direct impossible assets.

## 7. CTA gate

- One primary CTA.
- CTA follows delivered value.
- CTA connects to the current viewer intent.
- The final line maintains energy and clarity.

## QA summary

```text
PROMISE MATCH: PASS | REVISE
TRUTH: PASS | OPEN ITEMS
VOICE CONFIDENCE: HIGH | MEDIUM | LOW
RUNTIME: target / estimated
LOOPS: opened / closed
PRIMARY CTA COUNT: N
RETENTION RISKS: [hypotheses]
OPEN CONFIRMATIONS: [items]
```
