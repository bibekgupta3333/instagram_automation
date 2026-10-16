# Local setup: Apple Silicon Mac (M4 Pro, 24 GB)

This guide sets up the local video studio from
[docs/4-production/14-local-models-mac-m4.md](docs/4-production/14-local-models-mac-m4.md) ("doc 14").
All the scripts live in `scripts/setup/`, and every step has a `make` target.

> **Text runs on Bedrock / Claude Code, not locally.** Scripts, metadata and fact-checks go
> through `studio/llm` (`STUDIO_LLM=bedrock` or `claude-code`); set it up from
> [.env.example](.env.example). No local LLM (and no Ollama) is needed. Only **audio**
> (Kokoro TTS + parakeet captions) and **images** (mflux) run on the Mac.

```
make preflight  →  make tools  →  make deps-audio  →  make models-audio  →  make smoke
                                  (then, when disk allows: uv sync --extra local-image; make models STAGE=image)
```

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
| **~14 GB free disk** for audio | `df -h ~` | Audio runtime ~0.8 GB + audio models 2.9 GB + 10 GB buffer. Add 11 GB for the default image model (~25 GB total for tts + asr + image) |

Then set up the project:

```bash
uv sync                    # base deps + dev group (small)
cp .env.example .env       # STUDIO_LLM=bedrock|claude-code; AWS_PROFILE = a *personal* account only
make deps-audio            # = uv sync --extra local-audio --extra bedrock --group dev (~0.8 GB)
```

Extras: `local-audio` (mlx-audio, parakeet-mlx, misaki G2P + spaCy `en_core_web_sm`, no torch),
`local-image` (mflux), `local-ml` (both, kept for compatibility), `bedrock` (boto3).
`uv sync` installs exactly the extras you name, so keep `--extra bedrock` when adding audio.

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
The default pull set (`make models`) is **tts + asr + image**. Every LLM is optional because
text runs on Bedrock / Claude Code.

| Stage | Models | Download (GB) |
|---|---|---|
| tts | `mlx-community/Kokoro-82M-bf16` (safetensors only; 0.35 on disk) | **0.4** |
| asr | `mlx-community/parakeet-tdt-0.6b-v2` (CC-BY-4.0: credit NVIDIA; 2.3 on disk) | **2.5** |
| image | `mflux-community/z-image-turbo-mflux-q8` (11.0); optional `flux2-klein-4b-mflux-q8` (8.6) | **11.0** (+8.6 optional) |
| llm | all optional, text runs on Bedrock / Claude Code: `qwen3.5:9b` (7.6), `gemma4:12b` (7.7), `gpt-oss:20b` (14.0) | **0** (+29.3 optional) |
| music | optional `ACE-Step/Ace-Step1.5` (MIT) | **0** (+10.1 optional) |
| **Total (default: tts + asr + image)** | | **13.9** (61.9 with `--include-optional`) |
| Python `local-audio` extra (mlx 235 MB, llvmlite 125, scipy 71, transformers 55, spaCy + model 37, …) | | **~0.73** (measured) |
| Python `local-image` extra (mflux + deps) | | ~1–2 (est.) |
| Remotion + its headless Chromium (npm) | | ~0.5–1 (est.) |

Every pull needs the model size **plus 10 GB** free; preflight counts only models not yet
downloaded. The image models use 8-bit pre-quantised mflux builds. The upstream bf16 repos are
24–33 GB each.

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
make models STAGE=image               # Z-Image-Turbo q8 (11 GB) — needs 21 GB free
make models                           # the whole default set: tts + asr + image
make models STAGE=music OPTIONAL=1    # ACE-Step, when space allows
make models STAGE=llm OPTIONAL=1      # only for offline LLM experiments (needs make ollama)
```

- HF repos come through `hf download` (falling back to `huggingface-cli`, then
  `uvx --from huggingface_hub hf`) into the HF cache. Use
  `bash scripts/setup/20-pull-models.sh --dest models` to put them in `./models/<repo>` instead (git-ignored).
- Models that are already present are skipped.
- Runtimes: `make deps-audio` for audio; `uv sync --extra local-audio --extra local-image
  --extra bedrock --group dev` once there is room for mflux. For music, clone ACE-Step into
  `vendor/ACE-Step-1.5` (or set `ACE_STEP_DIR`) and run `uv sync` there.

### 4.4 `make smoke`

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

`make smoke-mock` runs `uv run studio make --mock` with no models. It SKIPs until the
`studio` CLI exists. Pass extra args with `make smoke-mock ARGS="..."`.

A real audio run of the whole pipeline (text still mock, images placeholder):
`uv run studio make --channel gravity-what-if --topic "..." --engine images=mock` with
`STUDIO_LLM` set in `.env`. Captions engines: `parakeet` (default, measures the audio),
`kokoro-timestamps` (reuses Kokoro's own word timings from `tts_words.json`, no second model)
and `mock`.

Measured on this Mac (M4 Pro 24 GB), 34 s short: Kokoro ~1.5 s load + ~1.7 s synthesis
(RTF ≈ 0.05), MLX peak ~1.9 GB; parakeet ~4 s first load, ~0.4–0.9 s per transcribe, MLX
peak ~4.4 GB; parakeet and Kokoro word starts agree to ~0.09 s on average.

Then do the doc 14 §9.6 week-1 benchmark: time 20 Kokoro minutes and 20 stills, then repeat
after 30 minutes of load to see the effect of heat.

## 5. GPU wired-memory limit (optional, you run it)

See [scripts/setup/40-gpu-limit.md](scripts/setup/40-gpu-limit.md). In short:

```bash
sysctl iogpu.wired_limit_mb                  # 0 = default (~16–18 GB on 24 GB)
sudo sysctl iogpu.wired_limit_mb=19456       # ~19 GB, until reboot
sudo sysctl iogpu.wired_limit_mb=0           # revert
```

Never set it above about 20 GB on a 24 GB Mac. Even with the limit raised, run **one heavy model at a time**.

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

| Symptom | Fix |
|---|---|
| Preflight `[FAIL] free X GB < required Y GB` | See §2. Pull one stage at a time; each model needs its size + 10 GB free |
| `20-pull-models.sh` exits 1 "Refusing to download" | Same as above. This is by design |
| `ollama server not running` | Only matters for optional local LLM pulls: `make ollama` |
| `ollama pull` slow or stalls | Re-run; pulls resume. Check `df -h` |
| Model runs at a few tokens/s, `ollama ps` shows CPU/GPU split | The model doesn't fit the wired limit. Use the 9B, close apps, see §5 |
| `MPS backend out of memory` (mflux, ACE-Step) | `ollama stop <model>` first; use `-q 8` or `-q 4`; raise the wired limit |
| ffmpeg lacks libass / `No such filter: 'ass'` | `brew reinstall ffmpeg`; make sure `/opt/homebrew/bin` comes first on `PATH` |
| `h264_videotoolbox` errors | Some sizes need even dimensions; use `-pix_fmt yuv420p`. Fall back to `-c:v libx264` |
| `venv cannot import studio` | `uv sync` from the repo root |
| `Kokoro requires the optional 'misaki' package` / spaCy `en_core_web_sm` missing | `make deps-audio` (the extra pins the spaCy model wheel; `spacy download` needs pip, which uv venvs lack) |
| `uv sync` removed boto3 or mflux | `uv sync` keeps only the extras you name: pass every one you need (`--extra local-audio --extra bedrock …`) |
| `tts: unknown Kokoro US voice(s)` | Use a voice from `KOKORO_US_VOICES` in `studio/stages/tts.py` (or `STUDIO_KOKORO_ANY_VOICE=1`) |
| `smoke-mock` SKIPs | The `studio` CLI (`studio/cli.py`) isn't built yet. This is expected for now |
| mflux/mlx-audio flag errors in smoke | Their CLIs change often. Check `--help` and adjust `scripts/setup/30-smoke-test.sh` |
| `hf download` 401/403 | The listed repos are public, so check `HF_TOKEN` isn't set to a stale value |
| AWS commands touch the wrong account | Set `AWS_PROFILE` in `.env` to a **personal** account. Never use an employer profile |
