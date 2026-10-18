# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Also read [AGENTS.md](AGENTS.md)**: the hard rules, agent roster (who owns which files) and parallel-work protocol for multi-agent work.

**Current installation status (2026-10-18):** project-installed local model weights
and Rhubarb's bundled speech models were removed at the user's request. Code,
dependencies, executables and generated media remain. Installed-stack descriptions
below describe the previous setup, not current model availability. Do not reinstall
models without an explicit new user request.

## What this is

A local-first AI video studio for 10 faceless channels (no avatar hosts; HeyGen was removed 2026-10-16) on YouTube, Instagram and Facebook, aimed at US viewers and AdSense revenue. Publishing goes through Buffer. The strategy, policy research and design decisions are in `docs/`, and `docs/README.md` is the index. Docs keep a stable number prefix (`04`, `12`, …), and code comments cite them as `doc 15 §5`.

## Commands

```bash
make deps        # = uv sync --extra local-audio --extra local-image --extra bedrock --group dev (uv sync DROPS unnamed extras)
make preflight                            # hardware/disk/ffmpeg/Ollama check (read-only)
make test                                 # uv run pytest -q   (all offline, ~20 s)
make guarded ARGS='make --channel car-cost-truth --topic "Why streamlined cars face less drag"'
make lint                                 # uv run ruff check .
uv run pytest -q tests/test_stages.py -k policy     # single test file / test
uv run studio channels                    # list the 10 brand packs
uv run studio make --channel gravity-what-if --topic "..." --mock   # offline end-to-end MP4
make guarded ARGS='make --channel X --topic "..." --engine llm=mock --engine images=mock'  # real audio
make guarded ARGS='batch --channel X --topics-file topics.txt'
uv run studio approve JOB_ID --by NAME    # human approval (binds to content hash)
uv run studio status [JOB_ID]
make models-dry                           # plan model downloads + disk check
make models                               # all non-optional manifest models (Configuration A, installed)
make gateway                              # LiteLLM proxy (infra/litellm/config.yaml)
```

`--mock` runs every stage without models or keys: a mock LLM, a sine-tone voice, evenly spaced captions and placeholder images. `--engine stage=name` overrides one stage (`llm`, `tts`, `captions`, `images`).

**Where each stage runs:**
- **Text** (script, metadata, polish, fact-check): **Bedrock** by default (`STUDIO_LLM=bedrock`). Auth is `AWS_PROFILE` (user fills it in) or `AWS_BEARER_TOKEN_BEDROCK`, plus `AWS_REGION`. Model IDs come from the git-ignored `.env`: `BEDROCK_MODEL_ID_FAST` (drafts, metadata, ideation; default Haiku 5.5), `BEDROCK_MODEL_ID_QUALITY` (polish, fact-check; default Sonnet 5.5), `BEDROCK_MODEL_ID_ESCALATION` (disputed claims; default Opus 5.5). Precedence: per-role `STUDIO_BEDROCK_MODEL_<ROLE>` > tier `BEDROCK_MODEL_ID_*` > built-in default; an empty value means the default. `STUDIO_LLM=claude-code` uses `claude -p` headless instead.
- **Audio**: local. Kokoro TTS via mlx-audio, and Parakeet word-timestamp captions via parakeet-mlx.
- **Visuals (Configuration A, installed):** `images=auto` works per beat. The baseline order tries **local Z-Image-Turbo 4-bit** first (mflux, `mflux-community/z-image-turbo-mflux-q4`, default 640×1152; `STUDIO_ZIMAGE_SIZE=576x1024` is the lower-memory override). Then Pixabay stock if `PIXABAY_API_KEY` is set (the key lives in the git-ignored `.env`, loaded by `studio/config.py`; Pixabay replaced Pexels 2026-10-16), then a mock placeholder marked "do not publish". Each raw still goes to `images/raw/` and is upscaled to frame size with **Real-ESRGAN x4plus** (`studio/stages/visuals/upscale.py`, spandrel on MPS, about 5 s). No Bedrock in the visuals path. For stock-suitable beats with a `stock_query` and no diagram/cutaway shot, `car-cost-truth`, `square-foot-money` and `business-model-files` try Pixabay → Z-Image → mock. Other beats use Z-Image → Pixabay → mock. An explicit `STUDIO_IMAGES_AUTO_ORDER` overrides this conditional routing; stock-first can still fall back to Z-Image.
- **Motion:** `STUDIO_MOTION=auto|parallax|zoompan`. `studio/stages/motion.py` is our own licence-clean 2.5D parallax, built on Depth Anything V2 **Small** (Apache), OpenCV remap and inpainting. It has 6 presets, rotated per job, and takes about 3.5 s per 4 s clip. It falls back to zoompan if the depth model is missing. `--mock` runs stay on zoompan unless `STUDIO_MOTION=parallax` is set.

## Architecture

The quality pass adds two-pass script direction, model uncertainty review for all
channels, shot/overlay/stock metadata, word captions, beat transitions and optional licensed
music. Review is not source verification: generated citations are never treated as evidence.
Use `make guarded` for real runs: it permits one guarded job at a time, limits CPU worker pools to 3 threads by default,
disables model downloads, and stops its process group when sampled macOS memory pressure
leaves green or disk is below 10 GB. It samples once per second, so brief spikes remain possible.
Use `STUDIO_ZIMAGE_SIZE=576x1024` for lower-memory image generation.
Z-Image uses mflux `--low-ram --vae-tile-size 256 --mlx-cache-limit-gb 0.25` by default:
single-seed component eviction and tiled VAE decoding reduce overlap and decoder peaks.
The cache limit bounds unused MLX buffers, not total RAM. Keep the external pressure guard;
background applications can change whether a given image size stays green.

- **`studio/contracts.py`** holds the shared Pydantic types (`BrandPack`, `Script`, `VideoJob`, `PublishRequest`, the `ChatClient` protocol). The pipeline and integration layers are written independently against it, so change it deliberately and update both sides.
- **The pipeline** is in `studio/stages/` and `studio/pipeline/runner.py`. The stages run in order, script → fact_check → tts → captions → images → render, and each is `run(job, brand, ctx) -> job`. Each stage is idempotent: it keeps a cache key per stage in `output/<job_id>/.cache/`, and job state persists to `output/<job_id>/job.json` after every stage. Job ids are deterministic, so a re-run resumes and skips stages it has already done. `run_batch` runs **stage by stage across jobs** rather than video by video, so only one heavy model is loaded at a time on the 24 GB Mac (doc 14 §9). Rendering is ffmpeg only (h264_videotoolbox, zoompan motion rotated per beat, burned-in ASS captions, loudnorm −14 LUFS).
- **The script stage runs a policy lint** (`studio/stages/policy.py`): credential words, income claims, and each brand's `red_lines` and `banned_templates`. These rules come from the YouTube AI-persona and inauthentic-content research in docs 11 and 17. Don't weaken them to make a test pass.
- **LLM layer (`studio/llm/`):** `routing.py` maps a *role* (draft, metadata, polish, fact_check…) to a Bedrock text model tier: fast (Haiku 5.5), quality (Sonnet 5.5), escalation (Opus 5.5), overridable from `.env` as above. `client.get_client()` picks `bedrock` (default), `claude-code`, `gateway` (LiteLLM), `ollama` (opt-in) or `mock` from `STUDIO_LLM`. `bedrock.py` uses the bearer API key and **ignores `AWS_PROFILE`/`~/.aws` when a key is set**. `budget.py` keeps a token bucket, monthly $ caps and a SQLite spend ledger. Quality roles **queue** (`BudgetDeferred`) when over budget; they never silently fall back to local.
- **Publishing (`studio/publish/`):** `gate.assert_publishable` is the hard gate. It requires `approved_hash == content_hash`, a public non-pre-signed https media URL, AI disclosure on and made-for-kids off. `buffer.py` is a GraphQL client that defaults to `DRY_RUN=1`, with idempotency per (job, platform). Long-form YouTube goes through `youtube.py`, not Buffer.
- **`studio/flows/`** holds the Prefect flows: text work by day, then an overnight GPU lane with concurrency 1.
- **`brand_packs/*.yaml`** has one per channel. Every channel receives model uncertainty review through the configured text backend, regardless of `accuracy_critical`; this does not verify sources.

## Rules for working here

- **Bedrock is text-only.** Don't add Bedrock image/video engines.
- **Never write an approval on the user's behalf.** `studio approve` must be run by a human, and agents must not set `approved_hash` or pass `--by` with the user's name.
- Never post for real or disable `DRY_RUN`. Agents may use the Buffer MCP in `.mcp.json` read-only only; see `infra/MCP.md`.
- Don't use the AWS profiles on this machine. They belong to the user's employer. Bedrock uses `AWS_PROFILE` from `.env`, which is empty by default.
- Disk is tight (about 10 GB free). Keep at least 10 GB free, and run `make models-dry` before any download. Model weights, `output/` and `.env` are git-ignored.
- mflux runs with `HF_HUB_OFFLINE=1`. The mflux engines refuse to run if their pre-quantized model isn't cached, and never pass `-q` (that pulls 16–33 GB). Local weights are currently removed; only reinstall after an explicit user request, through `scripts/setup/20-pull-models.sh` after its dry-run.
- MLX caches grow fast. The TTS and caption engines clear the MLX cache per segment and unload between stages. Keep that behaviour if you touch them.
- Several external API field names (Buffer GraphQL, Bedrock model IDs, mflux/mlx-audio CLI flags) are marked `unverified` in code and docs. Check them against the live API or the installed library before relying on them.
