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

## 2026-10-23 · Kids nursery-song pilot: Plinkety Pond (local only)
- Direction (user): a CoComelon-**style** channel (original, no Moonbug/Pinkfong IP) in UK English for the UK/EU and Australia/NZ, focused on virality.
- Docs: `docs/1-strategy/26-nursery-song-channel.md` (genre playbook, public-domain rhyme list with UK/EU/AU caveats, compliance: accurate made-for-kids, COPPA, UK Children's Code, GDPR Art 8, AU privacy, YouTube's 2025–26 enforcement against AI kids' content; realistic targets; 1B views is franchise-scale). `docs/2-channels/27-plinkety-pond-episode-01.{md,json}` (original song "Bubbles on My Tummy", 96 BPM C major, original melody, 24 shots = 60 s).
- Brand: `brand_packs/plinkety-pond.yaml`: `made_for_kids: true`, ages 2–4 with a caregiver (WHO), original otter family (Pebble, Fern, Mum, Bloop), voice `bf_emma`.
- Tool: `scripts/kids_song/` (`python -m scripts.kids_song.make --episode <json> --out <dir>`):
  - a deterministic numpy backing track
  - Kokoro vocal pitch-mapped to the melody (on pitch but **robotic**)
  - guarded FastWan clips conformed to 1080x1920 and 16:9
  - karaoke ASS captions (Fredoka, OFL, `assets/fonts/kids/`)
  - −14 LUFS; `run.json` with `made_for_kids: true`, `approved: false`, `published: false`
- tts: British voices `bf_*`/`bm_*` with Kokoro `lang_code="b"`; no US/UK blends. **tts VERSION 5.**
- Kokoro reinstalled (342 MB) for this user-requested video.
- Agents: **the publish gate still blocks made_for_kids=True by design.** Kids videos are local-only until a human reviews a kids publishing path; never mislabel to get past it. Adult channels keep "never kid-appealing". `references/channel-rules.md` is regenerated from the brand packs. Text-to-video can't keep characters consistent across shots, which is the main quality limit for this genre.

## 2026-10-23 · YouTube script skill + script quality gate
- Added: project skill `.claude/skills/youtube-script/`, vendored from the MIT-licensed `chaseraw/youtube-script-skill` (commit 7bfde3e; LICENSE and NOTICE kept). It outputs our `Script` JSON and adds our rules: faceless AI narration, no advice or credentials, brand red lines, no text in images, 150 wpm timing. `references/channel-rules.md` is generated from the brand packs, and a test fails if it goes stale. `scripts/validate_script.py` checks scripts and exits 0/1/2. The unlicensed skills we evaluated (valerymad, csalamida, Airpyk-98) were **not** copied.
- Added: `studio/stages/script_quality.py`, a deterministic gate run after polish covering the hook, beat count, runtime, pacing, the loop ending, overlay ≤5 words, visual-prompt specificity, text-in-image requests, repetition and an AI-cliché list. It runs one repair pass via role=polish, then fails the job and writes `script.rejected.json`. Script stage also has an angle step (`angle.json`; `STUDIO_SCRIPT_ANGLE=0` turns it off). **script VERSION 5.** The policy lint is unchanged and remains the hard gate.
- Aligned: skill hook limit is 4.8 s (≤ ~12 words), with the claim in the first ~5 words, the same as the pipeline gate.
- Docs: `docs/4-production/25-script-workflow.md` (topic intake, automated and skill paths, reviewer checklist, feedback loop).
- Real run (Bedrock gpt-oss-20b): both test scripts passed the gate, but they were **factually weak** ("heat cools the fuel", "car behaves like a wing lifting its weight"). The gate checks structure, not accuracy. A stronger QUALITY-tier model plus fact_check and human review are still needed.
- Agents: `studio make` cannot take a pre-written script yet. A `--script-file PATH` option is planned (Flows & Orchestration). Dependency sync failed offline (en-core-web-sm download), so tests ran with `uv run --no-sync`.

## 2026-10-23 · FastWan 1.3B fast local text-to-video (user request)
- Added: `scripts/generate_wan_video.py --preset fastwan-t2v`: stable-diffusion.cpp (pinned master-973-f0aac52, archive SHA-256 verified) with
  FastWan2.1-T2V-1.3B (3-step DMD distill, Apache-2.0), `lcm` sampler, sigmas `1.0,0.757,0.522,0.0`, cfg 1, tiny decoder.
  Allowed sizes are 320x576 and 480x832, frames 5–49 (4n+1). It rejects `--reference` and keeps every pilot guard (guarded/offline/3 threads, pinned digests, no overwrite).
  Manifest entry `fastwan2.1-t2v-1.3b-gguf` (roller777 GGUF, revision and hash pinned); it reuses `wan-umt5-q3` and `wan2.1-tiny-vae`. 21 offline tests are in `tests/test_wan_fastwan.py`.
- Why: the user asked for a WanGP-style but low-memory video generator at ≤1–2 min per clip. WanGP itself was rejected for this Mac: its MPS support is unofficial, it has open crash and noise bugs, every report is from a ≥32 GB Mac, and its smallest image-to-video setup is ~15 GB.
- Installed (explicit user request): 3.718 GB (FastWan 0.836 + UMT5 Q3 2.858 + TAEW2.1 0.023), plus the sd.cpp runtime (36 MB) in git-ignored `output/`. Free disk afterwards: 17.7 GB.
- Measured on M4 Pro under `make guarded`, all pressure samples green: 320x576 × 33 frames **44 s**; 320x576 × 49 frames **65 s**; 480x832 × 33 frames **106 s**. Max RSS ~5.8 GB.
- Quality: smooth camera motion and a cinematic look with no flicker, but **scene physics are unreliable** (mist like fountains, spillways in the wrong place). It is **text-to-video only**, so clips won't match our stills. Use it for short generic cutaways only, always with human review. 320x576 output needs upscaling.
- Agents: still not a pipeline stage (no images VERSION or contract change). The pull script fetches main while the generator verifies pinned hashes, so an upstream change stops the run before inference. Don't add WanGP or CUDA dependencies.

## 2026-10-18 · Project-installed model weights removed
- Removed at the user's request: Z-Image q4, Kokoro, Parakeet, Depth Anything Small,
  Real-ESRGAN, Wan VACE v3 Q4, UMT5 Q3 and both Wan codecs from their nine HF cache
  repositories; also removed the exclusively referenced ESRGAN shared blob and
  bundled Rhubarb Sphinx models plus the archive containing a second copy.
- Audit: **13.551 GB** of model/cache/archive files removed; free disk measured
  **30.419 GB** after removal. Inventory and exact-target deletion/verification records
  are in git-ignored `output/model-removal/`. No active guarded workload was present.
- Preserved: unrelated model/dataset caches, source, dependencies, executable runtimes,
  credentials and generated videos/images/audio. Rhubarb's executable remains but its
  speech resources are absent. Real local generation now requires model reinstallation.
- Agents: do not automatically reinstall models to repair missing-model checks. Wait
  for an explicit user request; retain offline rendering and the existing publish gates.
  Updated current-status guidance without erasing historical benchmark results.
- Verification: exact removal targets are absent and unrelated HF entries are retained;
  two agents assisted inventory, documentation and independent checks. No model loads,
  downloads, approvals or publishing were performed during removal.

## 2026-10-18 · Optional low-memory Wan reference-video pilot
- Added: exact `MODEL='name name'` selection for `make models-dry` / `make models`,
  with validation before downloads and safe handling of partially cached HF repositories.
  Default Configuration A selection is unchanged.
- Added: explicit optional Wan components (VACE 1.3B v3 Q4, UMT5 Q3, full or tiny
  codec) and `scripts/generate_wan_video.py`. The recommended `--decoder tiny` bundle
  is 4.227 GB; full is 4.459 GB; both installed together share files and total 4.481 GB.
  The standalone runner checks
  pinned cached-file digests, requires guarded/offline settings, limits pilot dimensions,
  uses Metal-compatible memory controls and refuses to overwrite media. It is not a new
  pipeline stage or a publishing integration; no stage VERSION or contracts changed.
- Why: the user explicitly requested a real local video model with a smaller footprint.
  This optional pilot is an exception to the earlier exhausted model-budget restriction;
  it does not enable downloads during rendering or alter default faceless production.
- Agents: the original calcuis VACE GGUF omits `vace_patch_embedding.weight` and fails
  before inference. Use the exact **v3** filename in the manifest; the incompatible file
  downloaded by this task was removed. The 6 GiB runtime budget is not total unified RAM.
  Retain the pressure guard, three threads and 10 GB disk floor. R2V is not audio lip-sync.
- Measured: same seeded 17-frame 320×384/20-step pilot completed with both codecs.
  Tiny took **264.98 s** versus full **1,693.05 s** (6.39× faster); all **268 / 1,686**
  pressure samples stayed green. Native process maximum RSS was 5.82 / 5.91 GB,
  not a measurement of total shared CPU/GPU memory. Both clips fully decoded and
  spaced frames were inspected; tiny is slightly softer. These are silent low-resolution
  pilots, not narration-synchronized presenters or finished episodes.
- Checks: **678 passed, 3 skipped** in the offline suite; Ruff and shell syntax passed.
  Five agents contributed setup, compatibility checks, documentation and offline tests.
  Guarded media results and runtime provenance are recorded in doc 24; no approval or
  publishing action was performed. Bedrock remains text-only.

## 2026-10-17 · Original portfolio script library (docs only)
- Added: `docs/6-script-library/`: 20 detailed treatments per adult channel plus 20 for the proposed kids pilot; each has exact opening/closing lines, five timed beats, an attention magnet, an independent short cut, two thumbnail concepts and episode-specific evidence/rights checks. Ten specialist agents contributed the adult files in two waves.
- Added: doc 22 directing/retention/thumbnail/rights guide and library index; linked from master index, portfolio, brands and production. These are treatments, not full timed narrations, completed experiments or cleared assets.
- Checks: 220 distinct episode IDs/titles, 1,980 required fields, 1,100 contiguous timed beats, 440 thumbnail concepts and 220 CSV catalog rows validated; 260 local documentation links resolve. Independent review caught and corrected a kids thumbnail/story mismatch. Documentation checks only; no runtime tests or media jobs needed.
- Agents: source registers are research pointers; verify exact claims and asset rights before production. No copyright or virality guarantees, no copied creator scripts/visuals, no runtime/config/model changes. Kids content remains local/unpublished under the current gate. No approval or posting performed.

## 2026-10-17 · Portfolio video lengths and virality (docs only)
- Added: doc 12 §2.1 table for all ten brands plus the proposed kids pilot: short-form and standard episode lengths, qualitative reach hypotheses and hooks.
- Agents: ratings are unvalidated editorial judgments, not guaranteed views or earnings. Validate lengths using retention; kids publishing remains blocked by the existing gate. No runtime or configuration changes.

## 2026-10-17 · Kids-channel opportunity research (docs only)
- Added: docs 19–21: sourced demand benchmarks, original Pocket Meadow Question Club brand and episodes, hypothetical ad-revenue sensitivity, experiment, production gaps and rights. Five parallel research agents contributed.
- Updated: master/folder indexes, portfolio, brands and roadmap; verified the announced February 2027 YPP changes against official guidance.
- Agents: preschool content must be accurately made for kids. Current publishing gate excludes it; this is a local-only proposal, not an enabled brand pack. No code, models, credentials, approvals or publishing changed; no guaranteed revenue or daily view claims.

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
