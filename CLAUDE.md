# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local-first AI video studio for 10 faceless or AI-avatar channels on YouTube, Instagram and Facebook, aimed at US viewers and AdSense revenue. Publishing goes through Buffer. The strategy, policy research and design decisions are in `docs/`, and `docs/README.md` is the index. Docs keep a stable number prefix (`04`, `12`, …), and code comments cite them as `doc 15 §5`.

## Commands

```bash
uv sync                                   # base deps (light); heavy ML: uv sync --extra local-ml
make preflight                            # hardware/disk/ffmpeg/Ollama check (read-only)
make test                                 # uv run pytest -q   (all offline, ~20 s)
make lint                                 # uv run ruff check .
uv run pytest -q tests/test_stages.py -k policy     # single test file / test
uv run studio channels                    # list the 10 brand packs
uv run studio make --channel gravity-what-if --topic "..." --mock   # offline end-to-end MP4
uv run studio batch --channel X --topics-file topics.txt [--mock]
uv run studio approve JOB_ID --by NAME    # human approval (binds to content hash)
uv run studio status [JOB_ID]
make models-dry                           # plan model downloads + disk check
make models                               # pull default (minimal, ~21.5 GB) local models
make gateway                              # LiteLLM proxy (infra/litellm/config.yaml)
```

`--mock` (or `--engine stage=mock`) runs every stage without models: a mock LLM, a sine-tone voice, evenly spaced captions and placeholder images. Use it to test pipeline changes.

## Architecture

- **`studio/contracts.py`** holds the shared Pydantic types (`BrandPack`, `Script`, `VideoJob`, `PublishRequest`, the `ChatClient` protocol). The pipeline and integration layers are written independently against it, so change it deliberately and update both sides.
- **The pipeline** is in `studio/stages/` and `studio/pipeline/runner.py`. The stages run in order, script → fact_check → tts → captions → images → render, and each is `run(job, brand, ctx) -> job`. Each stage is idempotent: it keeps a cache key per stage in `output/<job_id>/.cache/`, and job state persists to `output/<job_id>/job.json` after every stage. Job ids are deterministic, so a re-run resumes and skips stages it has already done. `run_batch` runs **stage by stage across jobs** rather than video by video, so only one heavy model is loaded at a time on the 24 GB Mac (doc 14 §9). Rendering is ffmpeg only (h264_videotoolbox, zoompan motion rotated per beat, burned-in ASS captions, loudnorm −14 LUFS).
- **The script stage runs a policy lint** (`studio/stages/policy.py`): credential words, income claims, and each brand's `red_lines` and `banned_templates`. These rules come from the YouTube AI-persona and inauthentic-content research in docs 11 and 17. Don't weaken them to make a test pass.
- **LLM layer (`studio/llm/`):** `routing.py` maps a *role* (draft, metadata, polish, fact_check…) to a model. Bulk work goes to local Ollama, quality work to Bedrock (doc 15). `client.get_client()` picks mock, the LiteLLM gateway, or direct Ollama from env. `budget.py` keeps a token bucket, monthly $ caps and a SQLite spend ledger. Quality roles **queue** (`BudgetDeferred`) when over budget; they never silently fall back to local.
- **Publishing (`studio/publish/`):** `gate.assert_publishable` is the hard gate. It requires `approved_hash == content_hash`, a public non-pre-signed https media URL, AI disclosure on and made-for-kids off. `buffer.py` is a GraphQL client that defaults to `DRY_RUN=1`, with idempotency per (job, platform). Long-form YouTube goes through `youtube.py`, not Buffer.
- **`studio/flows/`** holds the Prefect flows: text work by day, then an overnight GPU lane with concurrency 1.
- **`brand_packs/*.yaml`** has one per channel. `accuracy_critical: true` sends fact-checks to Bedrock.

## Rules for working here

- **Never write an approval on the user's behalf.** `studio approve` must be run by a human, and agents must not set `approved_hash` or pass `--by` with the user's name.
- Never post for real or disable `DRY_RUN`. Agents may use the Buffer MCP in `.mcp.json` read-only only; see `infra/MCP.md`.
- Don't use the AWS profiles on this machine. They belong to the user's employer. Bedrock uses `AWS_PROFILE` from `.env`, which is empty by default.
- Disk is tight. Run `make models-dry` before any download. Model weights, `output/` and `.env` are git-ignored.
- Several external API field names (Buffer GraphQL, Bedrock model IDs, mflux/mlx-audio CLI flags) are marked `unverified` in code and docs. Check them against the live API or the installed library before relying on them.
