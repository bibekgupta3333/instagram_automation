#!/usr/bin/env bash
# Standalone Linux Node/npm bootstrap; also used by the Ubuntu installer.
set -euo pipefail
case "${1:-}" in
  --dry-run)
    [[ $# == 1 ]] || { printf '%s\n' 'Use --dry-run without other arguments.' >&2; exit 2; }
    cat <<'EOF'
DRY RUN: no commands, downloads or writes will be executed.
Use an existing Node >=22 with npm, otherwise install the official Node 22 Linux
x64/ARM64 archive after checking SHA-256 against its official manifest.
Install under ~/.local/share/video-studio-tools; no sudo or shell-profile edits.
Activate with: export PATH="$HOME/.local/share/video-studio-tools/node/bin:$PATH"
EOF
    exit 0 ;;
  -h|--help)
    printf '%s\n' 'Usage: bash scripts/setup/05-node.sh [--dry-run]' \
      'Linux x86_64/ARM64. Requires curl, xz-utils, tar, sha256sum and 10 GiB free.'
    exit 0 ;;
  '') [[ $# == 0 ]] || exit 2 ;;
  *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
esac
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || die 'This bootstrap targets Linux. On macOS use your Node manager with .nvmrc (Node 22).'
(( EUID != 0 )) || die 'Run as a normal user, not root.'
export PATH="$HOME/.local/share/video-studio-tools/node/bin:$PATH"
if command -v node >/dev/null && command -v npm >/dev/null \
    && node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)'; then
  node --version
  npm --version
  exit 0
fi
for tool in curl tar xz sha256sum awk df; do
  command -v "$tool" >/dev/null || die 'Missing Linux tools; run sudo apt-get install curl ca-certificates xz-utils coreutils tar.'
done
case "$(uname -m)" in
  x86_64) node_arch=x64 ;;
  aarch64|arm64) node_arch=arm64 ;;
  *) die 'Supported architectures: x86_64 and aarch64.' ;;
esac
node_free_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
[[ "$node_free_kib" =~ ^[0-9]+$ ]] || die 'Cannot measure free disk.'
(( node_free_kib >= 11534336 )) || die 'Leave at least 10 GiB free plus Node installation headroom.'
node_tmp="$(mktemp -d)"
trap 'rm -rf -- "$node_tmp"' EXIT
download() { curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 "$1" -o "$2"; }
node_base=https://nodejs.org/download/release/latest-v22.x
download "$node_base/SHASUMS256.txt" "$node_tmp/SHASUMS256.txt"
node_file="$(awk -v arch="$node_arch" '$2 ~ ("^node-v22\\.[0-9]+\\.[0-9]+-linux-" arch "\\.tar\\.xz$") {print $2}' "$node_tmp/SHASUMS256.txt")"
[[ "$node_file" =~ ^node-v22\.[0-9]+\.[0-9]+-linux-(x64|arm64)\.tar\.xz$ ]] || die 'No unambiguous Node 22 archive in official manifest.'
node_version="${node_file#node-}"
node_version="${node_version%%-linux-*}"
download "https://nodejs.org/download/release/$node_version/$node_file" "$node_tmp/$node_file"
awk -v name="$node_file" '$2 == name {print}' "$node_tmp/SHASUMS256.txt" > "$node_tmp/node.sha256"
(cd "$node_tmp" && sha256sum --check node.sha256)
node_dir="$HOME/.local/share/video-studio-tools/${node_file%.tar.xz}"
mkdir -p "$node_dir"
tar -xJf "$node_tmp/$node_file" -C "$node_dir" --strip-components=1
node_link="$HOME/.local/share/video-studio-tools/node"
[[ ! -e "$node_link" || -L "$node_link" ]] || die "$node_link exists and is not a managed symlink."
ln -sfn "$node_dir" "$node_link"
node --version
npm --version
# Literal variable references are intentional for the copy/paste command.
# shellcheck disable=SC2016
printf '%s\n' 'Node ready. For the current shell:' \
  'export PATH="$HOME/.local/share/video-studio-tools/node/bin:$PATH"'
