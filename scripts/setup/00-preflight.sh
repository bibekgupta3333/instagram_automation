#!/usr/bin/env bash
# Preflight check for the local video studio on an Apple Silicon Mac (docs 14).
# Read-only: it installs nothing and changes nothing.
#
# Usage: scripts/setup/00-preflight.sh [--strict]
#   --strict   exit 1 if any check FAILs (default: always exit 0 after printing the report)
set -uo pipefail

# shellcheck source=scripts/setup/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

STRICT=0
for arg in "$@"; do
  case "$arg" in
    --strict) STRICT=1 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

echo "Studio preflight — $(date '+%Y-%m-%d %H:%M')  (repo: $REPO_ROOT)"

# --------------------------------------------------------------------------- hardware
section "Hardware"
OS_NAME="$(uname -s)"
if [[ "$OS_NAME" != "Darwin" ]]; then
  fail "not macOS ($OS_NAME). This stack targets Apple Silicon"
else
  info "macOS $(sw_vers -productVersion 2>/dev/null || echo '?')"
fi
CHIP="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo unknown)"
ARCH="$(uname -m)"
if [[ "$ARCH" == "arm64" && "$CHIP" == Apple* ]]; then
  pass "chip: $CHIP ($ARCH)"
else
  fail "chip: $CHIP ($ARCH). MLX needs Apple Silicon"
fi
MEM_BYTES="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
MEM_GB=$((MEM_BYTES / 1024 / 1024 / 1024))
if (( MEM_GB >= 24 )); then
  pass "RAM: ${MEM_GB} GB"
elif (( MEM_GB >= 16 )); then
  warn "RAM: ${MEM_GB} GB. docs 14 assumes 24 GB; use the smaller LLM only"
else
  fail "RAM: ${MEM_GB} GB. Too little for the local stack"
fi

# --------------------------------------------------------------------------- GPU wired limit
section "GPU wired-memory limit (read-only)"
WIRED="$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo unavailable)"
if [[ "$WIRED" == "unavailable" ]]; then
  warn "iogpu.wired_limit_mb not readable on this OS"
elif [[ "$WIRED" == "0" ]]; then
  info "iogpu.wired_limit_mb = 0 (system default, roughly 2/3-3/4 of RAM)"
  warn "not raised. Optional: see scripts/setup/40-gpu-limit.md (19456 for 24 GB)"
elif (( WIRED > 20480 )); then
  warn "iogpu.wired_limit_mb = $WIRED. Above ~20 GB risks freezes on a 24 GB Mac (docs 14 §10)"
else
  pass "iogpu.wired_limit_mb = $WIRED"
fi

# --------------------------------------------------------------------------- disk
section "Disk space vs models.manifest.yaml"
FREE="$(free_gb "$REPO_ROOT")"
if REQ="$(manifest total 2>/dev/null)" && BUF="$(manifest buffer 2>/dev/null)"; then
  REQ_OPT="$(manifest total --include-optional)"
  # Only models still to download count against free disk (present ones are already paid for).
  TODO=0
  while IFS=$'\t' read -r _ _ runtime source size _; do
    if [[ "$runtime" == "ollama" ]]; then ollama_running && ollama_has "$source" && continue
    else hf_has "$source" && continue; fi
    TODO="$(gb_add "$TODO" "$size")"
  done < <(manifest list)
  NEED="$(gb_add "$TODO" "$BUF")"
  info "free on $(df -P "$REPO_ROOT" | awk 'NR==2 {print $6}'): ${FREE} GB"
  info "models (required: tts + asr + image): ${REQ} GB, with optional: ${REQ_OPT} GB, buffer: ${BUF} GB"
  info "no local LLMs required: text runs on Bedrock / Claude Code (see .env.example)"
  while IFS=$'\t' read -r st rq op; do
    info "  stage ${st}: ${rq} GB$( [[ "$op" != "0.0" ]] && echo " (+${op} GB optional)")"
  done < <(manifest stages)
  if gb_lt "$FREE" "$NEED"; then
    fail "free ${FREE} GB < required ${NEED} GB (still to download ${TODO} + buffer ${BUF}). Free up disk first (SETUP.md §2)"
  else
    pass "free ${FREE} GB >= required ${NEED} GB (still to download ${TODO} + buffer ${BUF})"
  fi
else
  fail "could not read scripts/setup/models.manifest.yaml (needs PyYAML in .venv)"
fi

# --------------------------------------------------------------------------- tools
section "Tools"
if have brew; then pass "brew: $(brew --version 2>/dev/null | head -1)"; else fail "brew: missing (https://brew.sh)"; fi
if have uv; then pass "uv: $(uv --version 2>/dev/null)"; else fail "uv: missing (brew install uv)"; fi
if have node; then
  NODE_V="$(node --version)"; NODE_MAJOR="${NODE_V#v}"; NODE_MAJOR="${NODE_MAJOR%%.*}"
  if (( NODE_MAJOR >= 18 )); then pass "node: $NODE_V"; else warn "node: $NODE_V (Remotion wants >= 18)"; fi
else
  fail "node: missing (brew install node)"
fi
if have git; then pass "git: $(git --version | awk '{print $3}')"; else fail "git: missing"; fi
if have git-lfs; then pass "git-lfs: $(git-lfs --version | awk '{print $1}')"; else warn "git-lfs: missing (make tools)"; fi
if have rhubarb; then pass "rhubarb: present"; else warn "rhubarb (lip-sync, cartoon rig): missing. See make tools / SETUP.md"; fi
if have hf || have huggingface-cli; then
  pass "Hugging Face CLI: $(command -v hf || command -v huggingface-cli)"
else
  info "Hugging Face CLI not on PATH. 20-pull-models.sh falls back to 'uvx --from huggingface_hub hf'"
fi

# --------------------------------------------------------------------------- ffmpeg
section "FFmpeg"
if have ffmpeg; then
  pass "ffmpeg: $(ffmpeg -hide_banner -version 2>/dev/null | head -1 | awk '{print $3}')"
  ENCODERS="$(ffmpeg -hide_banner -encoders 2>/dev/null)"
  for enc in h264_videotoolbox hevc_videotoolbox; do
    if grep -q " $enc " <<<"$ENCODERS"; then pass "encoder $enc"; else fail "encoder $enc missing (hardware encode)"; fi
  done
  FILTERS="$(ffmpeg -hide_banner -filters 2>/dev/null)"
  if grep -qE ' (subtitles|ass) ' <<<"$FILTERS"; then
    pass "libass (subtitles/ass filters) for burned-in captions"
  else
    fail "ffmpeg built without libass. brew reinstall ffmpeg (Homebrew's build includes it)"
  fi
else
  fail "ffmpeg: missing (brew install ffmpeg)"
fi

# --------------------------------------------------------------------------- ollama
section "Ollama (optional: text runs on Bedrock / Claude Code)"
if have ollama; then
  OV="$(ollama --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | tail -1)"
  info "ollama installed: ${OV:-unknown} ($(command -v ollama)). Only needed for offline LLM experiments"
  if ollama_running; then
    info "ollama server running; local models: $(ollama list 2>/dev/null | awk 'NR>1 {print $1}' | tr '\n' ' ')"
  fi
else
  info "ollama not installed (not needed: text runs on Bedrock / Claude Code)"
fi

# --------------------------------------------------------------------------- python
section "Python project"
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PYV="$("$REPO_ROOT/.venv/bin/python" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
  pass ".venv present (Python $PYV)"
  if (cd "$REPO_ROOT" && "$REPO_ROOT/.venv/bin/python" -c 'import studio, studio.contracts' 2>/dev/null); then
    pass "venv imports studio (and studio.contracts)"
  else
    fail "venv cannot import studio. Run: uv sync"
  fi
  if (cd "$REPO_ROOT" && "$REPO_ROOT/.venv/bin/python" -c 'import studio.cli' 2>/dev/null); then
    pass "studio CLI module present"
  else
    warn "studio.cli not importable yet (CLI still being built). smoke-mock will SKIP"
  fi
  for mod in mlx_audio misaki en_core_web_sm parakeet_mlx; do
    if "$REPO_ROOT/.venv/bin/python" -c "import $mod" >/dev/null 2>&1; then
      pass "local audio: $mod importable"
    else
      warn "local audio: $mod missing. Run: uv sync --extra local-audio --extra bedrock --group dev"
    fi
  done
else
  fail "no .venv. Run: uv sync"
fi

summary
if (( N_FAIL > 0 )); then
  echo "Preflight found $N_FAIL failing check(s). See SETUP.md before 'make models'."
  (( STRICT )) && exit 1
fi
exit 0
