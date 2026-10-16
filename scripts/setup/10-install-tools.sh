#!/usr/bin/env bash
# Install the LIGHT system tools the studio needs, only if missing. Idempotent.
# It never downloads model weights, never uses sudo and never touches sysctl.
#
# Usage: scripts/setup/10-install-tools.sh [--dry-run] [--with-rhubarb]
#   --dry-run       print what would be installed, change nothing
#   --with-rhubarb  also fetch Rhubarb Lip Sync (~87 MB zip, ~200 MB unpacked) into
#                   ~/.local/share/rhubarb and link ~/.local/bin/rhubarb. Off by default
#                   because it is the only non-trivial download here.
set -euo pipefail

# shellcheck source=scripts/setup/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DRY=0; WITH_RHUBARB=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --with-rhubarb) WITH_RHUBARB=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

RHUBARB_VERSION="1.14.0"
RHUBARB_URL="https://github.com/DanielSWolf/rhubarb-lip-sync/releases/download/v${RHUBARB_VERSION}/Rhubarb-Lip-Sync-${RHUBARB_VERSION}-macOS.zip"

ACTIONS=0
# Keep brew from self-updating or upgrading other formulae (disk is tight).
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 HOMEBREW_NO_INSTALL_UPGRADE=1
run() {
  ACTIONS=$((ACTIONS + 1))
  if (( DRY )); then
    printf '  [DRY-RUN] would run: %s\n' "$*"
  else
    printf '  [RUN] %s\n' "$*"
    "$@"
  fi
}

if (( DRY )); then echo "Install light tools (dry run)"; else echo "Install light tools"; fi

section "Homebrew"
if ! have brew; then
  fail "Homebrew is missing. Install it from https://brew.sh (needs your password), then re-run"
  exit 1
fi
pass "brew present"

# command-name : brew-formula. Ollama is deliberately not here: it may come from the
# ollama.com app (as on this machine), and a second copy via brew would conflict.
TOOLS=(
  "ffmpeg:ffmpeg"
  "uv:uv"
  "node:node"
  "git:git"
  "git-lfs:git-lfs"
  "jq:jq"
)

section "CLI tools (brew install only what is absent)"
for pair in "${TOOLS[@]}"; do
  cmd="${pair%%:*}"; formula="${pair#*:}"
  if have "$cmd"; then
    pass "$cmd already installed ($(command -v "$cmd"))"
  else
    run brew install "$formula"
  fi
done

if have git-lfs && ! git config --global --get filter.lfs.process >/dev/null 2>&1; then
  info "git-lfs hooks not in your global git config. Run 'git lfs install' yourself if you want them"
fi

section "FFmpeg capabilities"
if have ffmpeg; then
  enc="$(ffmpeg -hide_banner -encoders 2>/dev/null)"; flt="$(ffmpeg -hide_banner -filters 2>/dev/null)"
  ok=1
  grep -q ' h264_videotoolbox ' <<<"$enc" || ok=0
  grep -q ' hevc_videotoolbox ' <<<"$enc" || ok=0
  grep -qE ' (subtitles|ass) ' <<<"$flt" || ok=0
  if (( ok )); then
    pass "ffmpeg has VideoToolbox (h264/hevc) and libass"
  else
    warn "ffmpeg lacks VideoToolbox or libass. Homebrew's ffmpeg has both"
    if brew list --formula ffmpeg >/dev/null 2>&1; then
      run brew reinstall ffmpeg
    else
      info "ffmpeg is not from Homebrew ($(command -v ffmpeg)). Remove that build or put /opt/homebrew/bin first, then: brew install ffmpeg"
    fi
  fi
elif (( DRY )); then
  info "ffmpeg capabilities will be checked after install"
fi

section "Ollama"
if have ollama; then
  pass "ollama present ($(command -v ollama)); start it with: make ollama"
else
  run brew install ollama
fi

section "Rhubarb Lip Sync (cartoon rig, CPU only)"
if have rhubarb; then
  pass "rhubarb present"
elif (( WITH_RHUBARB )); then
  dest="$HOME/.local/share/rhubarb"
  zip="${TMPDIR:-/tmp}/rhubarb-${RHUBARB_VERSION}.zip"
  run mkdir -p "$dest" "$HOME/.local/bin"
  run curl -fL --retry 2 -o "$zip" "$RHUBARB_URL"
  run unzip -q -o "$zip" -d "$dest"
  run ln -sf "$dest/Rhubarb-Lip-Sync-${RHUBARB_VERSION}-macOS/rhubarb" "$HOME/.local/bin/rhubarb"
  run rm -f "$zip"
  info "make sure ~/.local/bin is on PATH"
else
  skip "rhubarb not installed (no Homebrew formula; ~87 MB release zip). Re-run with --with-rhubarb when disk allows"
fi

section "Not installed here (heavy, on purpose)"
info "Remotion (npm, pulls Chromium): install inside the render project when disk allows"
info "local-ml extras (mlx-audio, parakeet-mlx, mflux): uv sync --extra local-ml, see SETUP.md"
info "model weights: scripts/setup/20-pull-models.sh"

echo
if (( ACTIONS == 0 )); then
  echo "Nothing to do: all light tools are present."
elif (( DRY )); then
  echo "Dry run: $ACTIONS action(s) would run. Re-run without --dry-run to apply."
else
  echo "Done: $ACTIONS action(s) run."
fi
