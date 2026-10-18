#!/usr/bin/env bash
# Download the local model stack listed in scripts/setup/models.manifest.yaml.
#
# Usage: scripts/setup/20-pull-models.sh [--stage llm|tts|asr|image|video|music|all]
#                                        [--dry-run] [--include-optional] [--dest hf-cache|models]
#                                        [--name NAME ...]
#   --stage             only that stage (default: all)
#   --dry-run           print the plan and the disk check, download nothing
#   --include-optional  also pull models marked optional (e.g. gemma4:12b)
#   --dest              where HF repos go: the shared HF cache (default) or ./models/<repo>
#   --name              exact model name, repeatable; optional names need --include-optional
#
# Before EACH model it re-checks free disk and exits 1 if free space is below
# (model size + buffer). Models already present are skipped, except HF include-file
# selections, which use the download client's file cache. LLMs go through
# `ollama pull` (server must be running: make ollama). HF repos go through `hf download`
# (or huggingface-cli, or `uvx --from huggingface_hub hf` as a fallback).
set -euo pipefail

# shellcheck source=scripts/setup/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

STAGE="all"; DRY=0; OPT=(); NAMES=(); DEST="hf-cache"
while (($#)); do
  case "$1" in
    --stage) STAGE="${2:-}"; shift 2 ;;
    --stage=*) STAGE="${1#*=}"; shift ;;
    --dry-run) DRY=1; shift ;;
    --include-optional) OPT=(--include-optional); shift ;;
    --name)
      if (( $# < 2 )) || [[ -z "$2" || "$2" == --* ]]; then
        echo "--name requires a model name" >&2; exit 2
      fi
      NAMES+=(--name "$2"); shift 2 ;;
    --name=*) NAMES+=(--name "${1#*=}"); shift ;;
    --dest) DEST="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$STAGE" in llm|tts|asr|image|video|music|all) ;; *) echo "bad --stage: $STAGE" >&2; exit 2 ;; esac
case "$DEST" in hf-cache|models) ;; *) echo "bad --dest: $DEST" >&2; exit 2 ;; esac

# Materialize and validate the complete list before any presence checks or downloads.
# Process substitution hides the producer's exit status from `set -e`.
ROWS="$(manifest list --stage "$STAGE" ${OPT[@]+"${OPT[@]}"} ${NAMES[@]+"${NAMES[@]}"})"
BUF="$(manifest buffer)"
TOTAL="$(manifest total --stage "$STAGE" ${OPT[@]+"${OPT[@]}"} ${NAMES[@]+"${NAMES[@]}"})"
FREE="$(free_gb "$REPO_ROOT")"

MODE=""; if (( DRY )); then MODE=" (DRY RUN)"; fi
echo "Model pull — stage=$STAGE dest=$DEST$MODE"
echo "Selected download size: ${TOTAL} GB   free now: ${FREE} GB   per-model buffer: ${BUF} GB"
echo

# shellcheck disable=SC2329  # invoked via "${cmd[@]}"
hf_cmd() {
  if have hf; then hf download "$@"
  elif have huggingface-cli; then huggingface-cli download "$@"
  else uvx --from huggingface_hub hf download "$@"
  fi
}

PLANNED=0; SKIPPED=0; PULLED=0; NEEDED_GB=0
while IFS=$'\t' read -r name stage runtime source size optional include verified; do
  [[ -z "$name" ]] && continue
  tags=""
  [[ $optional == yes ]] && tags+=", optional"
  [[ $verified == no ]] && tags+=", UNVERIFIED"
  label="[$stage] $name ($source, ~${size} GB${tags})"

  present=0
  if [[ "$runtime" == "ollama" ]]; then
    if ollama_running && ollama_has "$source"; then present=1; fi
  else
    # Repo presence does not prove that the selected include files are cached.
    # Let `hf download` resolve those files idempotently instead of skipping them.
    if [[ "$include" == "-" ]] && hf_has "$source"; then present=1; fi
  fi
  if (( present )); then
    skip "$label already present"
    SKIPPED=$((SKIPPED + 1)); continue
  fi

  if [[ "$runtime" == "ollama" ]]; then
    cmd=(ollama pull "$source")
  else
    args=("$source")
    if [[ "$DEST" == "models" ]]; then args+=(--local-dir "$REPO_ROOT/models/$source"); fi
    if [[ "$include" != "-" ]]; then
      IFS=',' read -r -a pats <<<"$include"
      args+=(--include "${pats[@]}")
    fi
    cmd=(hf_cmd "${args[@]}")
  fi

  FREE="$(free_gb "$REPO_ROOT")"
  need="$(gb_add "$size" "$BUF")"
  NEEDED_GB="$(gb_add "$NEEDED_GB" "$size")"
  if gb_lt "$FREE" "$need"; then
    fail "$label: free ${FREE} GB < ${need} GB needed (size + ${BUF} GB buffer)"
    if (( DRY )); then
      info "would run: ${cmd[*]/#hf_cmd/hf download}"
      info "dry run: a real run would refuse here (exit 1). Continuing the listing only"
      PLANNED=$((PLANNED + 1)); continue
    fi
    echo "Refusing to download: not enough disk. See SETUP.md §2 (free up disk first)." >&2
    exit 1
  fi

  if (( DRY )); then
    pass "$label: disk OK (free ${FREE} GB >= ${need} GB)"
    info "would run: ${cmd[*]/#hf_cmd/hf download}"
    PLANNED=$((PLANNED + 1)); continue
  fi

  if [[ "$runtime" == "ollama" ]] && ! ollama_running; then
    fail "$label: Ollama server is not running. Start it with: make ollama"
    exit 1
  fi
  info "pulling $label"
  "${cmd[@]}"
  pass "$label pulled"
  PULLED=$((PULLED + 1))
done <<< "$ROWS"

echo
echo "Models: $PLANNED planned, $PULLED pulled, $SKIPPED already present."
echo "Download size still needed: ${NEEDED_GB} GB (of ${TOTAL} GB selected)."
if (( DRY )) && (( N_FAIL > 0 )); then
  echo "A real run would REFUSE (exit 1): free disk is below the requirement."
  exit 1
fi
exit 0
