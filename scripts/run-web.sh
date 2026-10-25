#!/usr/bin/env bash
# Foreground launcher; inherits exported configuration, never sources .env.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$HOME/.local/share/video-studio-tools/node/bin:$PATH"
cd "$repo_dir"
if [[ ! -x .venv-web/bin/python ]]; then
  printf '%s\n' 'Missing .venv-web: run bash scripts/setup/50-ubuntu.sh (Ubuntu) or make deps-web.' >&2
  exit 1
fi
exec .venv-web/bin/python -m studio.web --host 127.0.0.1 --port 8080 "$@"
