#!/usr/bin/env bash
# Ubuntu web/CPU installation. Never installs ML weights or activates billing.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/setup/50-ubuntu.sh [options]
  --dry-run              Show the plan without executing commands (works on macOS).
  --skip-clis            Install only the project and system dependencies.
  --with-dev             Also install pytest and Ruff in .venv-web.
  --with-claude-desktop  Also install Claude Desktop for a graphical Ubuntu machine.
  --run                  Start the loopback web app after installation.
  -h, --help             Show help.
Run as your normal Ubuntu user with sudo access, from a source checkout.
EOF
}

dry_run=0
install_clis=1
with_dev=0
with_desktop=0
start_app=0
for argument in "$@"; do
  case "$argument" in
    --dry-run) dry_run=1 ;;
    --skip-clis) install_clis=0 ;;
    --with-dev) with_dev=1 ;;
    --with-claude-desktop) with_desktop=1 ;;
    --run) start_app=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$argument" >&2; usage >&2; exit 2 ;;
  esac
done

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if (( dry_run )); then
  cat <<'EOF'
DRY RUN: no commands, downloads or writes will be executed.
1. Check Ubuntu 22.04+, x86_64/aarch64, normal user, sudo and disk headroom.
2. apt: curl, certificates, Git, make, build tools, Python/venv, FFmpeg,
   espeak-ng, SQLite, ripgrep, jq, GnuPG, xz and fonts.
3. Install uv if missing; install Python 3.12 and the hash-pinned web profile
   in .venv-web. Do not run the Apple Silicon 'make deps' target on Ubuntu.
EOF
  if (( install_clis )); then
    cat <<'EOF'
4. Use Node >=22 or install an official Node 22 binary with SHA-256 verification.
5. Install GitHub CLI from its official apt repository.
6. Install Claude Code using https://claude.ai/install.sh (stable).
7. Install @openai/codex and opencode-ai@1.19.4 with npm into ~/.local.
   'claude' is the Claude Code CLI, not a second CLI package.
EOF
  fi
  (( with_desktop == 0 )) || printf '%s\n' 'Optional: install claude-desktop from the official Anthropic apt repository.'
  (( with_dev == 0 )) || printf '%s\n' 'Optional: install pytest and Ruff in .venv-web.'
  (( start_app == 0 )) || printf '%s\n' 'Then start scripts/run-web.sh on 127.0.0.1:8080.'
  printf '%s\n' 'No credentials read, logins, model weights, cloud billing or public listeners.'
  exit 0
fi

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || die 'This installer runs on Ubuntu; use --dry-run to inspect it elsewhere.'
[[ -f /etc/os-release ]] || die '/etc/os-release is required.'
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID%%.*}" =~ ^[0-9]+$ ]] || die 'Ubuntu 22.04+ is required.'
(( ${VERSION_ID%%.*} >= 22 )) || die 'Ubuntu 22.04+ is required.'
(( EUID != 0 )) || die 'Run as a normal user, not root or sudo bash; the script uses sudo for apt.'
command -v sudo >/dev/null || die 'sudo is required.'
[[ -f "$repo_dir/infra/web/requirements.lock" ]] || die 'Run from a complete source checkout.'
case "$(uname -m)" in
  x86_64|aarch64|arm64) ;;
  *) die 'Supported architectures: x86_64 and aarch64.' ;;
esac

check_disk() {
  local minimum_kib="${1:-10485760}" directory available
  for directory in "$repo_dir" "$HOME"; do
    available="$(df -Pk "$directory" | awk 'NR==2 {print $4}')"
    [[ "$available" =~ ^[0-9]+$ ]] || die 'Cannot measure disk availability.'
    (( available >= minimum_kib )) || die 'Insufficient disk: leave 10 GiB free plus installation headroom.'
  done
}
check_disk 12582912
sudo -v
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  ca-certificates curl git make build-essential pkg-config libffi-dev libssl-dev \
  python3 python3-venv ffmpeg espeak-ng sqlite3 ripgrep jq gnupg xz-utils fonts-dejavu-core
check_disk

setup_tmp="$(mktemp -d)"
trap 'rm -rf -- "$setup_tmp"' EXIT
download() { curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 "$1" -o "$2"; }
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/video-studio-tools"
export PATH="$HOME/.local/bin:$HOME/.local/share/video-studio-tools/node/bin:$PATH"
if ! command -v uv >/dev/null; then
  download https://astral.sh/uv/install.sh "$setup_tmp/uv-install.sh"
  UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$setup_tmp/uv-install.sh"
fi

if (( install_clis )); then
  bash "$repo_dir/scripts/setup/05-node.sh"
  check_disk

  # GitHub's maintained apt repository, with a repository-specific signing key.
  download https://cli.github.com/packages/githubcli-archive-keyring.gpg "$setup_tmp/githubcli.gpg"
  sudo install -d -m 0755 /etc/apt/keyrings
  sudo install -m 0644 "$setup_tmp/githubcli.gpg" /etc/apt/keyrings/githubcli-archive-keyring.gpg
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' \
    "$(dpkg --print-architecture)" > "$setup_tmp/github-cli.list"
  sudo install -m 0644 "$setup_tmp/github-cli.list" /etc/apt/sources.list.d/github-cli.list
  sudo apt-get update
  sudo apt-get install -y gh

  if ! command -v claude >/dev/null; then
    download https://claude.ai/install.sh "$setup_tmp/claude-install.sh"
    bash "$setup_tmp/claude-install.sh" stable
  fi
  # User-owned prefix: never sudo npm and never change the user's npm configuration.
  npm install --global --prefix "$HOME/.local" @openai/codex opencode-ai@1.19.4
  claude --version
  gh --version
  codex --version
  opencode --version
  check_disk
fi

if (( with_desktop )); then
  download https://downloads.claude.ai/claude-desktop/key.asc "$setup_tmp/claude-desktop.asc"
  sudo install -m 0644 "$setup_tmp/claude-desktop.asc" /usr/share/keyrings/claude-desktop-archive-keyring.asc
  printf '%s\n' 'deb [signed-by=/usr/share/keyrings/claude-desktop-archive-keyring.asc] https://downloads.claude.ai/claude-desktop/apt/stable stable main' > "$setup_tmp/claude-desktop.list"
  sudo install -m 0644 "$setup_tmp/claude-desktop.list" /etc/apt/sources.list.d/claude-desktop.list
  sudo apt-get update
  sudo apt-get install -y claude-desktop
  check_disk
fi

cd "$repo_dir"
uv python install 3.12
if [[ ! -e .venv-web ]]; then
  uv venv --python 3.12 .venv-web
fi
[[ -x .venv-web/bin/python ]] || die '.venv-web is not usable; do not copy virtual environments from a Mac.'
.venv-web/bin/python -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ required"'
uv pip install --python .venv-web/bin/python --require-hashes -r infra/web/requirements.lock
uv pip install --python .venv-web/bin/python --no-deps -e .
if (( with_dev )); then
  uv pip install --python .venv-web/bin/python pytest ruff
fi
mkdir -p "$HOME/.config/video-studio"
cat > "$HOME/.config/video-studio/env.sh" <<'EOF'
# PATH only: no credentials. Source from your shell if desired.
export PATH="$HOME/.local/bin:$HOME/.local/share/video-studio-tools/node/bin:$PATH"
EOF
check_disk
.venv-web/bin/python -c 'import sqlite3, fastapi, PIL; print("Web dependencies and Python SQLite: OK")'
# The printed command intentionally contains a literal $HOME for copy/paste.
# shellcheck disable=SC2016
printf '\n%s\n' 'Installed. Log in interactively if needed:' \
  '  source "$HOME/.config/video-studio/env.sh"' \
  '  gh auth login' '  claude' '  codex login --device-auth' '  opencode' \
  'Start: bash scripts/run-web.sh' 'Open: http://127.0.0.1:8080/create' \
  'No models downloaded. Cloud video remains disabled unless you configure and enable it.'
rm -rf -- "$setup_tmp"
trap - EXIT
if (( start_app )); then
  exec bash "$repo_dir/scripts/run-web.sh"
fi
