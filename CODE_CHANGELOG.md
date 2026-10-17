# CODE_CHANGELOG.md

Agent-facing log of **code** changes: what changed, why, and what it means for the next agent. It is newest first, one entry per commit or logical change. Docs-only research changes are summarised briefly; see `docs/` for the details.

**How to add an entry** (the Orchestrator does this when committing; see [AGENTS.md](AGENTS.md)):
```
## YYYY-MM-DD · <short-sha> · <title>
- Changed: <files/modules>: <what>
- Why: <decision or bug>
- Agents: <gotchas, new rules, migrations (VERSION bumps, env vars, contracts)>
```

---

## Unreleased (uncommitted work in progress)
- Changed: 22 files from the stopped quality-pass agents (script prompt v2, fact-check rewrite, captions/overlays, transitions, image prompt builder `visuals/prompts.py`, motion tweaks, `assets/` music/sfx folders, tests).
- Status: **unfinished and untested together.** Either review, finish and commit it, or discard it with `git restore . && git clean -fd assets studio/stages/visuals/prompts.py tests/test_render_edit.py`.
- Agents: don't build on these files until they are committed.

## 2026-10-17 · 4418335 · Add AGENTS.md
- Changed: `AGENTS.md` (new: hard rules, commands, architecture, agent roster, protocol, brief template), `CLAUDE.md` (points to AGENTS.md), `SETUP.md` (troubleshooting no longer suggests mflux `-q`).
- Agents: read AGENTS.md before editing, and edit only the paths your role owns.

## 2026-10-17 · 1f75060 · Beat gains optional shot / overlay / stock_query
- Changed: `studio/contracts.py` adds `Beat.shot`, `Beat.overlay` and `Beat.stock_query` (all optional, default "").
- Why: lets the script, visuals and edit work proceed independently. `overlay` text is drawn by ffmpeg and never inside AI images.
- Agents: producers (script) and consumers (images, render) may use these fields. Old jobs still load.

## 2026-10-17 · 9fe47a9 · Bedrock is text-only; model IDs from .env
- Changed:
  - `studio/llm/routing.py`, `budget.py`, `bedrock.py`: tier vars `BEDROCK_MODEL_ID_{FAST,QUALITY,ESCALATION}`. Precedence is `STUDIO_BEDROCK_MODEL_<ROLE>` > tier var > legacy > Claude 5.5 defaults, and empty means unset. Any id form works (base, `us.`/`global.` profile, ARN). Custom ids are priced by tier inference, with optional `BEDROCK_PRICE_*`. Non-Anthropic ids get no Anthropic-only fields and fall back to prompted JSON when tool use is unsupported.
  - `studio/stages/images.py`, `visuals/`: **removed `bedrock-stability` and `bedrock-luma`**. `auto` is now `z-image-turbo → pixabay → mock`, and removed names raise `RemovedEngineError`. images VERSION 7.
  - `studio/config.py`: the `.env` loader now treats `KEY=   # comment` as empty. The bug was that it read the comment text as the value, which would have sent it to Bedrock as a model ID.
- Agents: never add Bedrock image or video engines. The user's text model is `openai.gpt-oss-20b-1:0` for all tiers (verified working, including JSON schema output).

## 2026-10-16 · 11a14b9 · Pixabay replaces Pexels; HeyGen removed
- Changed:
  - `visuals/pixabay.py` (new; `pexels.py` deleted): videos first, then a photo fallback; 24 h search cache, rate-limit backoff, key redaction, attribution.
  - `studio/config.py`: a dependency-free `.env` loader that never overrides the env and is skipped under pytest.
  - Brand packs: Software Shortcut and Rank Test Lab are now `presenter: faceless`.
  - `policy.py`: the "claims to be human" rule now applies to **every** channel.
- Why: the user dropped the paid services. The user's key had been put in `.env.example` (tracked); it was moved to the git-ignored `.env`.
- Agents: never put real keys in `.env.example`. All channels are faceless.

## 2026-10-16 · 0122805 · Configuration A installed (local visuals)
- Changed:
  - `images.py`: local Z-Image-Turbo q4 first, at 576×1024, with `HF_HUB_OFFLINE=1` and the `.venv` mflux. Raw stills go to `images/raw/` and are upscaled.
  - `visuals/upscale.py` (new): Real-ESRGAN x4plus via spandrel on MPS.
  - `motion.py` (new): our own 2.5D parallax (Depth Anything V2 Small + OpenCV), 6 rotating presets, zoompan fallback.
  - `render.py`: VERSION 3, motion mode in the cache key.
  - `runner.py`: unloads the depth model after render.
  - Makefile: `deps` and `models` targets.
- Measured on M4 Pro: Z-Image takes ~80 s per image (12.6 GB footprint), ESRGAN ~5 s, parallax ~3.5 s per 4 s clip, and a 6-scene Short ~6.5 min.
- Agents: run one heavy model at a time, use `make deps` (never a bare `uv sync`), and keep the 12 GB model budget.

## 2026-10-16 · 6a4b9e3 · Fix mflux disk trap
- Changed: `images.py` uses pre-quantized `mflux-community/*-q4` repos and never `-q`. It refuses to run if the model isn't cached (`_hf_cached`, `STUDIO_ALLOW_MODEL_DOWNLOAD`). The manifest gained a `video` stage.
- Why: `-q 8` on a base id downloads 16–33 GB before quantizing.
- Agents: never pass `-q` to mflux, and download only through `scripts/setup/20-pull-models.sh`.

## 2026-10-16 · a85171f · Text to Bedrock / Claude Code; local audio
- Changed:
  - `studio/llm/`: Bedrock API-key auth (it **ignores AWS profiles when a key is set**), the `claude_code.py` headless backend, routing to Haiku/Sonnet/Opus, and a $30/month cap.
  - `tts.py` and `captions.py`: real Kokoro and Parakeet, with the MLX cache cleared per segment.
  - `runner.py`: single-video unload, plus `--engine llm=mock`.
  - `render.py`: two-pass loudnorm.
- Agents: the machine's `bamboo.*` AWS profiles belong to the employer, so never use them.

## 2026-10-16 · 2b31311 · Studio scaffold + docs reorganised
- Changed:
  - `studio/` pipeline: script → fact_check → tts → captions → images → render, with stage cache keys, deterministic job ids, and approval bound to `content_hash`.
  - `studio/llm` and `studio/publish`: the gate, Buffer dry-run and the YouTube stub.
  - `studio/flows`, `brand_packs/` (10 channels), and `scripts/setup`, `SETUP.md`, `Makefile`, `CLAUDE.md`.
- Agents:
  - Approvals are human-only.
  - `DRY_RUN=1` is the default.
  - Bump a stage `VERSION` when its output changes.

## 2026-10-16 · 91ec3b5 … 91de8b5 · Research docs (no code)
Strategy, policy, channels, the Buffer/Bedrock/MCP research, and the local model research (docs 01–18). Docs keep stable numbers, and code cites them as "doc NN §x".
