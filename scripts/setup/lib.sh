# shellcheck shell=bash
# Shared helpers for scripts/setup/*.sh. Source it, don't run it.

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SETUP_DIR/../.." && pwd)"
export SETUP_DIR REPO_ROOT

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_PASS=$'\033[32m'; C_WARN=$'\033[33m'; C_FAIL=$'\033[31m'; C_SKIP=$'\033[36m'; C_OFF=$'\033[0m'
else
  C_PASS=""; C_WARN=""; C_FAIL=""; C_SKIP=""; C_OFF=""
fi

N_PASS=0; N_WARN=0; N_FAIL=0; N_SKIP=0

pass() { N_PASS=$((N_PASS + 1)); printf '  %s[PASS]%s %s\n' "$C_PASS" "$C_OFF" "$*"; }
warn() { N_WARN=$((N_WARN + 1)); printf '  %s[WARN]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
fail() { N_FAIL=$((N_FAIL + 1)); printf '  %s[FAIL]%s %s\n' "$C_FAIL" "$C_OFF" "$*"; }
skip() { N_SKIP=$((N_SKIP + 1)); printf '  %s[SKIP]%s %s\n' "$C_SKIP" "$C_OFF" "$*"; }
info() { printf '  [INFO] %s\n' "$*"; }
section() { printf '\n== %s\n' "$*"; }

have() { command -v "$1" >/dev/null 2>&1; }

# Free space (GB, decimal, 1 dp) on the filesystem holding $1 (default: repo root).
free_gb() {
  local path="${1:-$REPO_ROOT}" kb
  kb="$(df -Pk "$path" | awk 'NR==2 {print $4}')"
  awk -v kb="$kb" 'BEGIN { printf "%.1f", kb * 1024 / 1e9 }'
}

# float compare: gb_lt A B  -> true if A < B
gb_lt() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a < b) }'; }
gb_add() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.1f", a + b }'; }

# Run the manifest reader with the project venv (PyYAML is a project dependency).
manifest() {
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    "$REPO_ROOT/.venv/bin/python" "$SETUP_DIR/manifest.py" "$@"
  elif have uv; then
    (cd "$REPO_ROOT" && uv run --quiet python "$SETUP_DIR/manifest.py" "$@")
  else
    python3 "$SETUP_DIR/manifest.py" "$@"
  fi
}

ollama_running() {
  curl -fsS -m 2 "${OLLAMA_BASE_URL:-http://localhost:11434}/api/version" >/dev/null 2>&1
}

# Is an Ollama tag present locally? (needs the server running)
ollama_has() {
  ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx -- "$1" \
    || ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx -- "$1:latest"
}

hf_cache_dir() {
  printf '%s' "${HF_HUB_CACHE:-${HF_HOME:-$HOME/.cache/huggingface}/hub}"
}

# Is an HF repo present, in ./models/<repo> or the HF cache?
hf_has() {
  local repo="$1" cache
  cache="$(hf_cache_dir)/models--${repo//\//--}"
  [[ -d "$REPO_ROOT/models/$repo" && -n "$(ls -A "$REPO_ROOT/models/$repo" 2>/dev/null)" ]] && return 0
  [[ -d "$cache/snapshots" && -n "$(ls -A "$cache/snapshots" 2>/dev/null)" ]]
}

summary() {
  printf '\nSummary: %d PASS, %d WARN, %d FAIL, %d SKIP\n' "$N_PASS" "$N_WARN" "$N_FAIL" "$N_SKIP"
}
