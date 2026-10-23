# Notice

This skill is adapted from **youtube-script-skill** by Chase Yang, MIT License
(full text in `LICENSE`).

- Upstream: https://github.com/chaseraw/youtube-script-skill
- Commit: `7bfde3eafaf58368d1d1c611cbf4496f1be03654` (committed 2026-09-15)
- Imported: 2026-10-23

## Changes from upstream

- `SKILL.md`: rewritten for this studio's 10 faceless, AI-voiced channels. Keeps the
  upstream process (brief, angle, evidence ledger, hooks, retention, spoken text, reconcile,
  quality gates) and adds studio rules: never claim to be human, no credentials or
  health/legal/finance/politics advice, brand `red_lines` and `banned_templates`, no text in
  AI images (labels in `overlay`), 150 wpm timing, Shorts of 35-50 s with 8-12 beats, a
  1-2 s hook, loop endings, original substance, affiliate disclosure. Output is JSON for
  `studio.contracts.Script` instead of a Markdown script.
- `references/*.md`, `templates/*.md`: upstream text kept verbatim, with a "Studio
  adaptation" note (references) or a "Studio JSON mapping" section (templates) added.
- `references/channel-rules.md`, `scripts/build_channel_rules.py`: new; generated from
  `brand_packs/*.yaml`.
- `scripts/validate_script.py`: rewritten to validate Script JSON (schema, timing, beats,
  overlays, image prompts, clichés, credential/advice language, evidence, disclosure) and to
  call the pipeline policy lint when `studio` is importable.
- `scripts/validate_markdown.py`: upstream `validate_script.py` for Markdown planning
  drafts, unchanged except the default WPM (145 -> 150) and ruff fixes (`re.I` -> `re.IGNORECASE`).
- `templates/short.json`, `examples/wing-and-turbine-short.json`, `tests/bad-script.json`:
  new. `tests/upstream-sample-script.md` is upstream `tests/sample-script.md`, unchanged.
- Dropped: upstream `README.md` and `.gitignore`.
