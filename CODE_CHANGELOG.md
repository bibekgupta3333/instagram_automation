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

## 2026-10-17 · Lower-memory local Z-Image execution
- Changed: `studio/stages/images.py` now uses mflux `--low-ram --vae-tile-size 256
  --mlx-cache-limit-gb 0.25` for Z-Image only; images VERSION **11** invalidates prior
  caches. Flux commands, cached q4 enforcement, offline generation and pressure guards
  remain unchanged. No weights, dependencies or operating-system limits were changed.
- Why: installed mflux 0.23 supports component eviction before decoding, smaller VAE
  tiles and an unused-buffer cache limit; the project command did not enable these controls.
  The old yellow-pressure incident's detailed logs were deleted, so its precise peak phase
  cannot be established retrospectively.
- Initial evidence: isolated 384x672 generation took 27.91 seconds with 30/30 readings green;
  576x1024 with low-RAM mode took 57.94 seconds with 60/60 readings green. Tests exercise
  the real command builder with mocked processes, including cached q4/offline/single-seed
  requirements and unchanged Flux arguments.
- Pipeline validation: two real 640x1152 images, each followed by Real-ESRGAN upscaling to
  1080x1920, completed in 163.15 seconds with **165/165** pressure readings green and at
  least 15.91 GB free disk. All four generated images across three guarded runs were local;
  no Bedrock, Pixabay, approval or publishing calls were used.
- Checks: **616 passed, 3 skipped** in the full offline suite; Ruff and diff checks passed.
- Agents: cache limits bound unused MLX buffers, not total RAM. Multiple seeds can retain
  components, so keep one seed per subprocess. Continue using `make guarded`; other apps
  can change available headroom. Do not claim these observations guarantee future pressure.

## 2026-10-17 · Quality pass completed; guarded macOS execution
- Changed: finished the interrupted script/visual/audio/edit work. Scripts use draft and
  polish passes with shot directions, overlays and stock queries. Every channel gets model
  uncertainty review; quantities without supplied evidence are rejected, and model-generated
  citations are explicitly not verified sources. No policy rules were weakened.
- Visuals: centralized no-text/shot-aware prompts, stock-first real-world channels, automotive
  search anchors and metadata filtering, sharper crop selection, repeat avoidance and cache
  corrections. Local q4 models only; no downloads or additional providers.
- Edit/audio: word captions and overlay cards, short shots and beat transitions, voice
  compression, optional licensed music ducking and SFX, streamed TTS, and cache-correct
  shot-directed parallax. Mock/silence cleanup no longer initializes GPU libraries.
- Resources: `scripts/run_guarded.py` / `make guarded ARGS='make ...'` permits one guarded
  job, caps CPU worker pools at 3 threads, disables model downloads and samples macOS
  pressure once per second. Non-green pressure, unreadable telemetry or disk below 10 GB
  stops only the launched process group (exit 75). Brief inter-sample spikes remain possible.
- Cleanup: batches unload their current model even on unexpected backend exceptions.
- Security: Pixabay asset URLs and every redirect must use exact approved HTTPS hosts;
  credentials, private destinations and nonstandard ports are refused before download.
- Fact-check false-positive fix: qualitative "one factor" is not a measured figure;
  percentages, multipliers and measured counts remain blocked. Source verification is still
  distinct from a model's vote.
- Live validation: a source-checked editorial storyboard rendered a 45.37-second stock-only
  preview at 1080x1920, -14.0 LUFS / -1.5 dBFS true peak. All 35 sampled pressure readings
  were green; minimum free disk was 13.09 GB. Preview is not approved or published.
  A prior Z-Image run was stopped by the guard when pressure left green; do not claim local
  image generation stayed green, or bypass the guard to make it finish.
- Checks: final offline suite **608 passed, 3 skipped**; Ruff and diff checks passed;
  documentation audit checked 210 local links without broken paths or fragments.
- Agents: versions are script **4**, fact_check **5**, images **10**, motion **3**, render **5**,
  captions/TTS **4**. New controls: `STUDIO_CPU_THREADS`, `STUDIO_ZIMAGE_SIZE/STEPS`,
  `STUDIO_STOCK_FIRST_CHANNELS`, `STUDIO_TTS_VOICE/SPEED`, `STUDIO_MUSIC/MUSIC_MOOD`.
  Preserve streamed audio, model unloading, offline tests and the human publishing gate.

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
