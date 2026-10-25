---
name: youtube-script
description: Write, rewrite or review scripts for the studio's 10 faceless channels (YouTube Shorts, Instagram/Facebook Reels and long-form YouTube) as pipeline-ready Script JSON. Use when asked to draft a script, hook, beat sheet or loop ending for any brand pack (gravity-what-if, wing-and-turbine, megabuild-blueprint, car-cost-truth, square-foot-money, business-model-files, furnace-to-faucet, lock-screen-logic, rank-test-lab, software-shortcut), or to audit a draft for promise mismatch, unsupported claims, generic AI voice, pacing, text-in-image prompts, credential or advice language, red-line breaches or a missing affiliate disclosure.
---

# YouTube Script (studio edition)

Adapted from [chaseraw/youtube-script-skill](https://github.com/chaseraw/youtube-script-skill)
(MIT; see `LICENSE` and `NOTICE.md`). The upstream process is kept: **brief → angle →
evidence → hooks → retention structure → spoken text → reconcile → quality gates**. What
changes is the creator: every channel here is a **faceless brand with an AI-generated
voice**, and the output is **JSON for `studio.contracts.Script`**, not a Markdown script.

Keep planning artifacts short and internal. Never speak framework words ("hook", "loop",
"beat") in narration.

## Route

- **Shorts / Reels** (default, `--format short`): 35–50 s, 8–12 beats.
- **Long-form** (`--format long`): 8–10 min, 40–70 beats, 4–6 chapters.
- **Audit**: an existing script JSON (often `output/<job_id>/script.json`). Run the
  validator first, keep good lines, make targeted edits.
- **Hook only**: brief → hook cards → QA. No full script.

## Required inputs

Proceed on explicit, low-risk assumptions; ask only when the answer changes the script.

- **Channel slug.** Read its section in `references/channel-rules.md` (generated from
  `brand_packs/*.yaml`, which stays the source of truth). Stay strictly inside its niche.
- Topic or source material, viewer situation and knowledge level, one takeaway.
- **Evidence.** Any number, statistic, cost, date or quote needs supplied, dated evidence.
  With topic-only input, write qualitative causal explanations and return `sources: []`.
  Never invent statistics, quotes, sources, URLs, credentials or personal experience.
- Affiliate or sponsor involvement, if any.

## Studio rules (override upstream defaults)

1. **Faceless, AI-voiced narrator.** No name, no persona, no "I worked on", "as a …",
   "when I …". Never claim to be human or deny being AI. The voice profile is the brand
   pack (niche, hook styles, banned phrases), not a creator's transcripts; upstream's
   voice-artifact step becomes "read the brand section" (`references/creator-voice.md`).
2. **No advice, no credentials.** Explain mechanisms; never tell the viewer what to buy,
   sell, sign, take, sue over or vote for. No credential words for the narrator (Dr., CFP,
   CPA, attorney, pilot, electrician, contractor, expert, advisor, guru, hacker…).
   Health, legal, finance and politics stay explainers (doc 11 §6–8). A disclaimer does not
   fix advice; rewrite the line instead. Don't mention a red-line topic even to deny it.
3. **Brand red lines and banned templates** from the channel's pack. A violation fails the
   video in the pipeline lint (`studio/stages/policy.py`).
4. **No text in AI images.** `visual_prompt` describes objects only: no text, words,
   letters, labels, captions, signs, logos, brands, insignia, numbers or people. Labels go
   in `overlay` (1–5 words, drawn by ffmpeg). Don't paste the brand style; it is appended.
5. **Timing at ~150 wpm** (scaled by the brand's voice speed). Shorts: 35–50 s total
   (about 90–125 spoken words across hook + beats + cta), 8–12 beats of 6–14 words, a new
   picture every 2.5–4 s. **The hook's claim lands in the first 1–2 s** (the first ~5 words); the whole hook stays ≤ ~12 words (4.8 s at 150 wpm), the same limit the pipeline gate uses: a
   concrete claim or causal question, never "In this video", "Did you know", "Imagine", a
   greeting or the channel name. **The cta is a loop line** (4–10 words) that ends
   mid-thought and flows back into the hook. No subscribe ask, outro or thanks.
6. **Original substance, not templates** (YouTube inauthentic-content policy, docs 11/17).
   Each video walks through 3–5 real mechanisms in order, with an original angle,
   narrative or our own data (Rank Test Lab experiments, Software Shortcut screens). No
   reused third-party clips, no lists of unrelated facts, no recycled scripts.
7. **Affiliate disclosure.** If any link, code or sponsor is involved, say it in the
   narration before the endorsement ("If you use our links, we earn a commission") and put
   it in `description`. Lender links on Square Foot Money: say they may be affiliate links.
8. US English, short spoken sentences (≤ 14 words), US customary units, no AI clichés
   ("basically", "mind-blowing", "game-changer", "let that sink in", "here's the thing",
   "you won't believe", "delve", "buckle up"…).

## Workflow

1. **Brief** (`references/brief-and-angle.md`): viewer, tension, promise, one takeaway,
   proof mechanism, differentiation thesis. One video, one transformation.
2. **Evidence ledger**: every factual claim as VERIFIED (supplied source + date) or OPEN.
   OPEN claims are cut from the JSON or reworded qualitatively. Generated citations are
   never evidence.
3. **Hooks** (`references/retention.md`): three different cards (payoff, tension,
   demonstration), adapted from the brand's hook styles, never copied from them. Pick on
   promise match and available proof, not shock.
4. **Beat sheet**: hook → stakes (beats 1–2) → 3–5 escalating mechanism steps → payoff
   that answers the hook → loop cta. Per beat choose a `shot` from `wide, medium, close-up,
   macro detail, aerial, cutaway, diagram`: no shot twice in a row, ≥ 4 distinct shots,
   at least one macro detail and one diagram or cutaway.
5. **Write the JSON** (`templates/shorts.md`, `templates/long-form.md`): beat `text` is the
   exact narration; `visual_prompt` is 25–45 words (subject, setting, viewpoint, lighting);
   `stock_query` is 2–4 lowercase keywords, empty for diagram/cutaway.
6. **Reconcile**: hook promise = payoff; every beat advances the takeaway; no line a
   generic competitor would say unchanged; channel red lines respected.
7. **Validate** (`references/quality-gates.md`), then fix and re-run until PASS:

   ```bash
   uv run python .Codex/skills/youtube-script/scripts/validate_script.py script.json \
       --channel wing-and-turbine --format short
   ```

   It checks the schema (and the real pydantic model), hook ≤ 4.8 s (≤ ~12 words), beat count, total
   duration, overlay ≤ 5 words, text-in-image wording and numbers in `visual_prompt`, AI
   clichés, credential/human/advice language, quantities without sources, affiliate
   disclosure, loop cta, title and hashtags, and runs the pipeline policy lint (brand red
   lines and banned templates). Exit code 1 means FAIL. Don't predict retention numbers.

## Output contract

Return the script as one JSON object matching `studio.contracts.Script` (no extra keys):

```json
{"title": "", "hook": "", "beats": [{"text": "", "visual_prompt": "", "shot": "",
 "overlay": "", "stock_query": ""}], "cta": "", "sources": [], "description": "",
 "hashtags": []}
```

- `title` < 70 chars, same promise as the hook. `description`: 2–3 sentences, the
  platform caption, with any required disclosure. `hashtags`: 3–6, each `#word`.
- `sources`: only supplied evidence actually used; `[]` for topic-only input.

After the JSON, add (unless the user asked for JSON only): consequential assumptions, open
confirmations, and a QA summary (validator result, runtime, loop open/close, CTA count).

Compact example (wing-and-turbine, full file in `examples/wing-and-turbine-short.json`,
10 beats, ~49 s, passes the validator):

```json
{
  "title": "Why Most Jet Engine Thrust Never Touches the Fire",
  "hook": "Most thrust bypasses the fire.",
  "beats": [
    {"text": "A big airliner engine is mostly one giant fan up front.", "shot": "wide",
     "visual_prompt": "Generic unbranded airliner turbofan engine seen from the front on a wing, huge fan disc filling the frame, night ramp setting, low worm's-eye view with a wide lens, cool rim light outlining the fan blades",
     "overlay": "Mostly fan", "stock_query": "jet engine fan"},
    {"text": "Only a small stream goes inside, into the compressor and combustor.",
     "shot": "cutaway",
     "visual_prompt": "Cutaway turbofan engine sliced lengthwise, a narrow inner airflow stream highlighted as it enters the compressor stages and the combustion chamber, side elevation view, clean studio lighting with a faint orange glow at the core",
     "overlay": "Small core stream", "stock_query": ""}
  ],
  "cta": "That is why, on every flight,",
  "sources": [],
  "description": "Why a modern turbofan gets most of its push from a giant fan instead of the fire in its core. Narrated with an AI-generated voice.",
  "hashtags": ["#aviation", "#jetengine", "#howitworks", "#engineering"]
}
```

## Running it through the pipeline

1. Save the JSON outside the repo's tracked paths and outside `output/` (e.g.
   `$TMPDIR/<channel>-<slug>.json`) and validate it as above.
2. **There is no pre-written-script option in `studio make` yet** (`studio/cli.py` takes
   `--channel --topic --format --mock --engine --force` only, and the script stage always
   drafts and polishes from the topic). Until that lands, use the validated JSON as the
   reference for the topic and review, and run the normal path:

   ```bash
   uv run studio make --channel wing-and-turbine --topic "Why most jet thrust bypasses the fire" --mock
   make guarded ARGS='make --channel wing-and-turbine --topic "Why most jet thrust bypasses the fire"'
   ```

   Compare `output/<job_id>/script.json` with your JSON by running the validator on it.
   Don't hand-write files or cache keys under `output/` to inject a script.
3. A human reviews the render and runs `studio approve`. Agents never approve or publish.

## Failure conditions

Stop and revise when the script:

- states a number, cost or comparison without supplied, dated evidence;
- has a hook the payoff doesn't deliver, or a hook that opens on setup;
- gives advice, implies a credential, or lets the narrator claim to be human;
- crosses a channel red line or uses a banned template, even to deny it;
- asks an image model for text, labels, logos or numbers;
- reads like an essay, piles up unrelated facts, or could be any channel's script;
- ends on a subscribe ask instead of a loop line, or has more than one CTA;
- mentions an affiliate link or sponsor without a spoken and written disclosure;
- copies another creator's distinctive expression or a brand hook example verbatim.
