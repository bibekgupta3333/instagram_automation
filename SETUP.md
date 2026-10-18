# Local setup: Apple Silicon Mac (M4 Pro, 24 GB)

> **Current status — 2026-10-18:** local model weights installed for this project
> were removed at the user's request, including bundled Rhubarb speech models and
> the archive containing them. Code, dependencies, runtime executables and generated
> media remain. Do not run model download/install steps unless the user explicitly
> requests reinstalling models. The stack descriptions below document the earlier setup.

This guide sets up the local video studio from
[docs/4-production/14-local-models-mac-m4.md](docs/4-production/14-local-models-mac-m4.md) ("doc 14").
All the scripts live in `scripts/setup/`, and every step has a `make` target.

> **Text runs on Bedrock / Claude Code, not locally.** Scripts, metadata and fact-checks go
> through `studio/llm` (`STUDIO_LLM=bedrock` or `claude-code`); set it up from
> [.env.example](.env.example). No local LLM (and no Ollama) is needed. Only **audio**
> (Kokoro TTS + parakeet captions) and **images** (mflux) run on the Mac.

```
make preflight  →  make tools  →  make deps  →  make models-dry  →  make models
```

This machine previously ran **Configuration A** (§1a): local audio, local Z-Image-Turbo
4-bit stills, Real-ESRGAN upscaling and depth parallax. Its weights are now removed.

Every channel receives model uncertainty review through the configured text backend.
This is not source verification: model-generated citations are not trusted evidence.
Use guarded execution for real media (§8); never raise GPU limits to force a render.

---

## 1. Prerequisites

| Need | Check | Notes |
|---|---|---|
| Apple Silicon Mac, macOS 15+ | `uname -m` → `arm64` | Doc 14 targets an M4 Pro with 24 GB |
| Homebrew | `brew --version` | https://brew.sh |
| git, Python ≥ 3.12, uv | `uv --version` | `brew install uv` |
| Node ≥ 18 | `node --version` | Remotion renderer |
| FFmpeg with VideoToolbox and libass | `make preflight` | Homebrew's `ffmpeg` has both |
| Ollama ≥ 0.40 (optional) | `ollama --version` | Only for offline LLM experiments; text runs on Bedrock / Claude Code |
| **~23 GB free disk** for Configuration A | `df -h ~` | Python deps ~1.6 GB + models 9.0 GB (tts + asr + image + video) + 10 GB buffer. mflux also needs swap headroom: macOS swap files share the disk |

Then set up the project:

```bash
cp .env.example .env       # STUDIO_LLM=bedrock|claude-code; AWS_PROFILE = a *personal* account only
make deps                  # = uv sync --extra local-audio --extra local-image --extra bedrock --group dev
```

Extras: `local-audio` (mlx-audio, parakeet-mlx, misaki G2P + spaCy `en_core_web_sm`),
`local-image` (mflux, opencv-python-headless, spandrel; pulls torch for Real-ESRGAN on MPS),
`local-ml` (both, kept for compatibility), `bedrock` (boto3).
**`uv sync` removes every extra you don't name**, so a bare `uv sync` or `uv sync --extra
bedrock` uninstalls mflux and the audio stack. Use `make deps` (`make deps-audio` is now an
alias for it). `uv run` does not prune, so it is safe.

### Bedrock settings in `.env` (text only)

Bedrock serves **text models only** (no image or video). Set these in the git-ignored `.env`:

| Variable | Used for | Empty means |
|---|---|---|
| `AWS_PROFILE` *or* `AWS_BEARER_TOKEN_BEDROCK` | Auth: a **personal** AWS profile, or a Bedrock API key | — (one is required) |
| `AWS_REGION` | Bedrock Runtime region | set it (`.env.example` uses `us-east-1`) |
| `BEDROCK_MODEL_ID_FAST` | Drafts, metadata, ideation | Haiku 5.5 |
| `BEDROCK_MODEL_ID_QUALITY` | Polish and fact-check | Sonnet 5.5 |
| `BEDROCK_MODEL_ID_ESCALATION` | Disputed claims | Opus 5.5 |
| `STUDIO_BEDROCK_MODEL_<ROLE>` (optional) | Pin one role, e.g. `STUDIO_BEDROCK_MODEL_DRAFT` | the tier above |

Precedence: per-role `STUDIO_BEDROCK_MODEL_<ROLE>` > tier `BEDROCK_MODEL_ID_*` > built-in default.
An empty value means the default. Accepted ID formats (check the exact IDs in your Bedrock console):

```bash
BEDROCK_MODEL_ID_FAST=anthropic.claude-haiku-5-5            # base model id (in-Region)
BEDROCK_MODEL_ID_QUALITY=us.anthropic.claude-sonnet-5-5     # cross-Region profile (us. or global. prefix)
BEDROCK_MODEL_ID_ESCALATION=arn:aws:bedrock:us-east-1:123456789012:inference-profile/global.anthropic.claude-opus-5-5   # inference-profile ARN
```

## 1a. Configuration A (what is installed)

| Layer | What | Size on disk | Licence |
|---|---|---|---|
| Stills | `mflux-community/z-image-turbo-mflux-q4` (transformer 3.46 + Qwen3 TE 2.27 + VAE 0.17 GB) | 5.9 GB (5.5 GiB `du`) | Apache-2.0 |
| Upscale | Real-ESRGAN x4plus (`Comfy-Org/Real-ESRGAN_repackaged`, one file) | 0.07 GB | BSD-3 |
| Parallax | Depth Anything V2 Small | 0.1 GB | Apache-2.0 |
| Audio | Kokoro 82M + parakeet-tdt-0.6b-v2 | 2.6 GB | Apache-2.0 / CC-BY-4.0 |
| Python | `local-audio` + `local-image` extras (mflux 0.23, torch with MPS, opencv, spandrel) | ~1.6 GB | |

```bash
make deps                                  # Python runtimes (see the uv warning above)
make models-dry                            # plan + disk check
make models                                # the manifest's default set (all non-optional models)
make models STAGE=image                    # Z-Image q4 + Real-ESRGAN only
make guarded ARGS='make --channel megabuild-blueprint --topic "..." --engine llm=mock'  # real audio + images
```

How the images stage uses it (`studio/stages/images.py`): For stock-suitable beats with a `stock_query` and no diagram/cutaway shot, `car-cost-truth`, `square-foot-money` and `business-model-files` try Pixabay → Z-Image → mock. Other beats use Z-Image → Pixabay → mock. An explicit `STUDIO_IMAGES_AUTO_ORDER` overrides this conditional routing; stock-first can still fall back to Z-Image. Mock placeholders are marked "do not publish". There is no Bedrock
image or video engine: Bedrock is used for text only. Z-Image renders a raw still at
**640×1152 by default** (1152×640 long-form); use `STUDIO_ZIMAGE_SIZE=576x1024`
for the lower-memory override (1024×576 long-form). Stills go into `images/raw/`, then Real-ESRGAN x4 makes the
1080×1920 frame. A guarded lower-resolution invocation is:

```bash
.venv/bin/python scripts/run_guarded.py -- .venv/bin/mflux-generate-z-image-turbo \
  --model mflux-community/z-image-turbo-mflux-q4 --steps 9 \
  --low-ram --vae-tile-size 256 --mlx-cache-limit-gb 0.25 \
  --width 576 --height 1024 --seed 42 --prompt "... no text, no logos" --output out.png
```

- The repo id alone is enough: mflux infers `z-image-turbo` from the name (no `--base-model`).
  `--guidance` and `--negative-prompt` are ignored (guidance-distilled). Sizes must be
  multiples of 16.
- **Never** add `-q 4` or use the built-in `z-image-turbo` name: both pull the 32.9 GB base
  repo. The stage runs mflux with `HF_HUB_OFFLINE=1` and refuses if the repo is not in the HF
  cache. Never set `STUDIO_ALLOW_MODEL_DOWNLOAD=1`; downloads belong in the setup pull script.
- Measured on this M4 Pro 24 GB (`/usr/bin/time -l`, output/benchmarks/zimage/): 576×1024 is
  ~79–84 s per still (9 steps × ~7.1 s + ~15 s load), 720×1280 is ~96–130 s. Peak memory
  footprint 12.6 GB (576×1024) to 15.5 GB (720×1280). These historical measurements do not
  guarantee a green-pressure run; respect guard stops and use stock-only footage when needed.
- Updated low-RAM validation (2026-10-17): a 576×1024 still took **57.94 s**, with **60/60**
  memory samples green. Two sequential default-size 640×1152 images plus Real-ESRGAN
  upscaling to 1080×1920 took **163.15 s**, with **165/165** samples green. These are observed
  runs with current background load, not guaranteed RAM limits. The 0.25 GB setting limits
  unused MLX cache buffers, not total model or system memory. No system GPU limits changed.

## 2. Free up disk first

`make preflight` FAILs when free space is below **models + 10 GB buffer**. The pull script
re-checks before **each** model and refuses (exit 1) when space runs short. Find what is
using space first:

```bash
df -h ~
du -sh ~/.cache/huggingface ~/.ollama ~/Library/Caches 2>/dev/null
du -sh ~/Library/Developer ~/Library/Containers/com.docker.docker 2>/dev/null   # Xcode / Docker
du -sh ~/Downloads ~/.npm ~/.cache/uv 2>/dev/null
ollama list                                   # each model's size
```

Common ways to reclaim space (review first; these scripts never delete anything):

- `ollama rm <model>` for models you no longer use
- `hf cache scan`, then `hf cache delete`, for old Hugging Face snapshots
- `uv cache prune`, `npm cache clean --force`, `brew cleanup --prune=all`
- `docker system df`, then `docker system prune` (removes stopped containers and dangling images)
- Xcode: delete `~/Library/Developer/Xcode/DerivedData` and old simulators (`xcrun simctl delete unavailable`)

Optionally, keep HF models on an external SSD: `export HF_HOME=/Volumes/SSD/hf` and
`export OLLAMA_MODELS=/Volumes/SSD/ollama` before you pull. Preflight measures the disk that
holds the repo, so check the SSD yourself with `df -h /Volumes/SSD`.

## 3. Disk needed per stage

Generated from `scripts/setup/models.manifest.yaml`. Every listed model is licensed for commercial use.
The default pull set (`make models`) is **tts + asr + image + video (depth)**. Every LLM is optional because
text runs on Bedrock / Claude Code.

| Stage | Models | Download (GB) |
|---|---|---|
| tts | `mlx-community/Kokoro-82M-bf16` (safetensors only; 0.35 on disk) | **0.4** |
| asr | `mlx-community/parakeet-tdt-0.6b-v2` (CC-BY-4.0: credit NVIDIA; 2.3 on disk) | **2.5** |
| image | `mflux-community/z-image-turbo-mflux-q4` (5.9) + Real-ESRGAN x4plus (0.07); optional `flux2-klein-4b-mflux-q4` (4.6) | **6.0** (+4.6 optional) |
| video | Depth Anything V2 Small (0.1); optional FastWan / Wan / LingBot GGUFs | **0.1** |
| llm | all optional, text runs on Bedrock / Claude Code: `qwen3.5:9b` (7.6), `gemma4:12b` (7.7), `gpt-oss:20b` (14.0) | **0** (+29.3 optional) |
| music | optional `ACE-Step/Ace-Step1.5` (MIT) | **0** (+10.1 optional) |
| **Total (default: every non-optional model)** | | **9.0** |
| Python `local-audio` extra (mlx 235 MB, llvmlite 125, scipy 71, transformers 55, spaCy + model 37, …) | | **~0.73** (measured) |
| Python `local-image` extra (mflux, torch, opencv, spandrel) | | ~0.8 (measured) |
| Remotion + its headless Chromium (npm) | | ~0.5–1 (est.) |

Every pull needs the model size **plus 10 GB** free; preflight counts only models not yet
downloaded. The image models use 4-bit pre-quantised mflux builds. The upstream bf16 repos are
16–33 GB each.

## 4. Steps

### 4.1 `make preflight`

This is read-only. It reports PASS/WARN/FAIL for:

- chip and RAM
- free disk against the manifest (models still to download + 10 GB; no LLMs)
- brew, uv, node and ffmpeg versions
- the VideoToolbox h264/hevc encoders and libass
- Ollama (informational only; not needed)
- `iogpu.wired_limit_mb`
- whether `.venv` imports `studio` and the local audio packages

Use `bash scripts/setup/00-preflight.sh --strict` to exit 1 on any FAIL (for CI).

### 4.2 `make tools` (or `make tools-dry` first)

This is idempotent. It runs `brew install` only for missing light tools (ffmpeg, uv, node, git,
git-lfs, jq) and checks ffmpeg for VideoToolbox and libass. It never installs Ollama via brew
if Ollama is already installed (for example from the app). Rhubarb (lip-sync for the cartoon rig) has no
Homebrew formula. Add it with `bash scripts/setup/10-install-tools.sh --with-rhubarb`
(~87 MB zip into `~/.local/share/rhubarb`).

### 4.3 `make models`

```bash
make models-dry                       # plan + disk check, downloads nothing
make models-audio                     # Kokoro (0.4 GB) then parakeet (2.5 GB)
make models STAGE=image               # Z-Image-Turbo q4 (5.9 GB) + Real-ESRGAN — needs ~16 GB free
make models                           # the manifest's default set (every non-optional model)
make models STAGE=music OPTIONAL=1    # ACE-Step, when space allows
make models STAGE=llm OPTIONAL=1      # only for offline LLM experiments (needs make ollama)
```

For the explicitly requested neural-video pilot, select exact names rather than
all optional video repositories (some contain many quantizations):

```bash
make models-dry STAGE=video OPTIONAL=1 MODEL='wan2.1-vace-1.3b-q4 wan-umt5-q3 wan2.1-tiny-vae'
make models STAGE=video OPTIONAL=1 MODEL='wan2.1-vace-1.3b-q4 wan-umt5-q3 wan2.1-tiny-vae'
```

The recommended tiny-codec weights total 4.23 GB. Include `--decoder tiny` when
running the standalone generator. See [doc 24](docs/4-production/24-local-wan-video-pilot.md)
for the external Metal runtime, offline guarded command and measured comparison:
4.4 minutes with the tiny codec versus 28.2 minutes with the full VAE for the same
1.06-second pilot; all recorded pressure samples stayed green.
This optional experiment is separate from the default Configuration A stack.

- HF repos come through `hf download` (falling back to `huggingface-cli`, then
  `uvx --from huggingface_hub hf`) into the HF cache. Use
  `bash scripts/setup/20-pull-models.sh --dest models` to put them in `./models/<repo>` instead (git-ignored).
- Whole repositories that are already present are skipped. Exact-file selections
  are resolved through the downloader's cache so a partial repository cannot hide a missing file.
- Runtimes: use `make deps` (audio + image + bedrock + dev). Optional external music
  runtimes are separate from the project environment; do not sync a reduced set of extras here.

### 4.4 `make smoke`

For real smoke work, use the guard: `.venv/bin/python scripts/run_guarded.py -- make smoke`.
The `make guarded` target wraps the studio CLI, so it cannot wrap a Make target directly.

This renders a 5-second 1080×1920 test clip through each stage: (optional local LLM hook) →
Kokoro voice → parakeet word timings → Z-Image still → ACE-Step bed → FFmpeg render with
burned-in libass captions, encoded with `h264_videotoolbox`. The TTS and ASR stages call the
pipeline's own engines (`studio.stages.tts` / `captions`) and report the real-time factor and
how closely parakeet's word starts match Kokoro's own timings.

- If a stage's model or tool is missing, the stage is **SKIPPED** with a reason. It doesn't fail.
- The render stage always runs, using placeholders for any skipped inputs. That proves
  FFmpeg, libass and VideoToolbox work.
- Add `--stage <name>` to run one stage: `bash scripts/setup/30-smoke-test.sh --stage render`.
- To run the music stage, set `ACE_STEP_CMD` to the repo's generate command, which must write `$OUT_WAV`.

`make smoke-mock` runs the existing `studio` CLI with `--mock` and no models.
Pass extra args with `make smoke-mock ARGS="..."`.

A real audio run of the whole pipeline (text still mock, images placeholder):
`make guarded ARGS='make --channel gravity-what-if --topic "..." --engine llm=mock --engine images=mock'`. Captions engines: `parakeet` (default, measures the audio),
`kokoro-timestamps` (reuses Kokoro's own word timings from `tts_words.json`, no second model)
and `mock`.

Measured on this Mac (M4 Pro 24 GB), 34 s short: Kokoro ~1.5 s load + ~1.7 s synthesis
(RTF ≈ 0.05), MLX peak ~1.9 GB; parakeet ~4 s first load, ~0.4–0.9 s per transcribe, MLX
peak ~4.4 GB; parakeet and Kokoro word starts agree to ~0.09 s on average.

Then do the doc 14 §9.6 week-1 benchmark: time 20 Kokoro minutes and 20 stills, then repeat
after 30 minutes of load to see the effect of heat.

## 5. GPU memory safety

Keep the system GPU memory limit unchanged and run **one heavy model at a time** through
the guard. Never raise the wired limit or bypass a resource stop to force a render.
Use `STUDIO_ZIMAGE_SIZE=576x1024` as a lower-memory override; if pressure cannot remain
green, use stock-only footage. Stock-first routing alone still permits local image generation.

## 6. Ollama and the MLX backend (optional; doc 14 §3)

Not part of the default setup: text runs on Bedrock / Claude Code (see `.env.example`).
Keep this only for offline LLM experiments.

- From **Ollama ≥ 0.40** (v0.40.0, 2026-09-25), supported architectures (qwen3.5/3.6/3.8, gemma4) run
  **on MLX by default** on Apple Silicon. The March 2026 preview required "> 32 GB", so
  **check that MLX is really used on 24 GB**.
- `make ollama` starts `ollama serve` on 127.0.0.1 with `OLLAMA_MAX_LOADED_MODELS=1`,
  `OLLAMA_KEEP_ALIVE=10m` and `OLLAMA_CONTEXT_LENGTH=16384`. Never bind it to 0.0.0.0. If you
  use the menu-bar app instead, set these with `launchctl setenv ...` and restart the app.
- Test it:

  ```bash
  ollama run qwen3.5:9b --verbose "Write a 12-word hook about compound interest."
  ollama ps                                  # SIZE, PROCESSOR (want 100% GPU)
  curl -s localhost:11434/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model":"qwen3.5:9b","messages":[{"role":"user","content":"Say OK"}]}'
  ```

  The doc 14 estimate for qwen3.5:9b is about 27–32 tokens/s. A much lower eval rate suggests
  it isn't on MLX, or is swapping. The fallback is
  `mlx_lm.server --model mlx-community/Qwen3.5-9B-4bit --host 127.0.0.1 --port 8081`.
  Explicit MLX tags also exist (`qwen3.5:9b-mlx`, `gemma4:12b-mlx`).
- Free memory before the image stage with `ollama stop gpt-oss:20b`.
- `make gateway` starts the LiteLLM proxy with `infra/litellm/config.yaml` via `uvx`. The first run
  downloads LiteLLM (a few hundred MB).

## 7. Where outputs land

| What | Where |
|---|---|
| Rendered videos and pipeline output | `$STUDIO_OUTPUT_DIR` (default `output/`, git-ignored) |
| Smoke test artefacts | `output/smoke/` (`smoke.mp4`, `captions.ass`, `voice.wav`, `still.png`, `*.log`) |
| Ollama models | `~/.ollama/models` (or `$OLLAMA_MODELS`) |
| HF models | `~/.cache/huggingface/hub` (or `$HF_HOME`), or `./models/` with `--dest models` |

## 8. Troubleshooting

For foreground-friendly work on macOS, use the guarded launcher:

```bash
make guarded ARGS='make --channel car-cost-truth --topic "Why streamlined cars face less drag"'
```

It permits one guarded job at a time, sets CPU worker pools to 3 threads, keeps model
downloads offline, and checks macOS memory pressure and free disk every second. It stops
only its own process group if pressure leaves green, disk drops below 10 GB, or telemetry
cannot be read. Exit code 75 means the run was stopped or refused; re-run after resources
recover to resume cached stages. Samples are in `output/memory-pressure.jsonl`.
This is a sampled safeguard, not a guarantee against brief spikes or other apps using RAM.
Three worker threads are an approximate background-work setting, not a hard 25% CPU cap.
Keep Z-Image at `STUDIO_ZIMAGE_SIZE=576x1024` for lower memory use; set this in the shell
for a run rather than changing global macOS GPU limits.

| Symptom | Fix |
|---|---|
| Preflight `[FAIL] free X GB < required Y GB` | See §2. Pull one stage at a time; each model needs its size + 10 GB free |
| `20-pull-models.sh` exits 1 "Refusing to download" | Same as above. This is by design |
| `ollama server not running` | Only matters for optional local LLM pulls: `make ollama` |
| `ollama pull` slow or stalls | Re-run; pulls resume. Check `df -h` |
| Model runs at a few tokens/s, `ollama ps` shows CPU/GPU split | The model doesn't fit the wired limit. Use the 9B, close apps, see §5 |
| `MPS backend out of memory` (mflux, ACE-Step) | Stop other heavy jobs, render at 576x1024, and use the guarded launcher. Do not raise the wired limit to force a workload through. **Never** use mflux `-q` (it downloads the 16–33 GB base model) |
| ffmpeg lacks libass / `No such filter: 'ass'` | `brew reinstall ffmpeg`; make sure `/opt/homebrew/bin` comes first on `PATH` |
| `h264_videotoolbox` errors | Some sizes need even dimensions; use `-pix_fmt yuv420p`. Fall back to `-c:v libx264` |
| `venv cannot import studio` | `make deps` from the repo root |
| `Kokoro requires the optional 'misaki' package` / spaCy `en_core_web_sm` missing | `make deps` (the extra pins the spaCy model wheel; `spacy download` needs pip, which uv venvs lack) |
| `uv sync` removed boto3 or mflux | Run `make deps` to restore all required extras |
| `tts: unknown Kokoro US voice(s)` | Use a voice from `KOKORO_US_VOICES` in `studio/stages/tts.py` (or `STUDIO_KOKORO_ANY_VOICE=1`) |
| `smoke-mock` SKIPs | Check that setup is complete and run `make deps`; the `studio` CLI exists |
| mflux/mlx-audio flag errors in smoke | Their CLIs change often. Check `--help` and adjust `scripts/setup/30-smoke-test.sh` |
| `hf download` 401/403 | The listed repos are public, so check `HF_TOKEN` isn't set to a stale value |
| AWS commands touch the wrong account | Set `AWS_PROFILE` in `.env` to a **personal** account. Never use an employer profile |
