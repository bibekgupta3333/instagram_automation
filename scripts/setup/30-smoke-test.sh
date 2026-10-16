#!/usr/bin/env bash
# Smoke test: render a 5-second test clip through each local stage (docs 14 §9.6).
#
# Usage: scripts/setup/30-smoke-test.sh [--stage llm|tts|asr|image|music|render|all]
#        scripts/setup/30-smoke-test.sh --mock [-- extra args for `studio make`]
#
# Real mode: every stage whose model/tool is missing is SKIPPED with a reason, never
# FAILED. A stage FAILs only when its model is present but the run breaks. The render
# stage always runs: it uses whatever earlier stages produced, or ffmpeg-generated
# placeholders, and burns in captions with libass + h264_videotoolbox.
#
# --mock: runs `uv run studio make --mock ...` (no models). SKIPs if the studio CLI
# is not ready yet.
#
# Outputs: $STUDIO_OUTPUT_DIR/smoke/ (default: output/smoke/). Exit 1 only on FAIL.
set -uo pipefail

# shellcheck source=scripts/setup/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

STAGE="all"; MOCK=0; MOCK_ARGS=()
while (($#)); do
  case "$1" in
    --stage) STAGE="${2:-}"; shift 2 ;;
    --stage=*) STAGE="${1#*=}"; shift ;;
    --mock) MOCK=1; shift ;;
    --) shift; MOCK_ARGS=("$@"); break ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

OUT_ROOT="${STUDIO_OUTPUT_DIR:-output}"
[[ "$OUT_ROOT" = /* ]] || OUT_ROOT="$REPO_ROOT/$OUT_ROOT"
OUT="$OUT_ROOT/smoke"
mkdir -p "$OUT"
PY="$REPO_ROOT/.venv/bin/python"
DUR=5
SMOKE_TEXT="This is a five second smoke test for the local studio."

model_field() { # model_field <stage> -> TSV row of the first required model for that stage
  manifest list --stage "$1" | head -1
}
py_has() { [[ -x "$PY" ]] && "$PY" -c "import $1" >/dev/null 2>&1; }
want() { [[ "$STAGE" == "all" || "$STAGE" == "$1" ]]; }
timed() { # timed <label> cmd...  -> runs, prints seconds
  local t0 t1 rc
  t0=$(date +%s)
  "${@:2}"; rc=$?
  t1=$(date +%s)
  info "$1 took $((t1 - t0)) s"
  return $rc
}

# --------------------------------------------------------------------------- mock mode
if (( MOCK )); then
  echo "Smoke test (mock) — output: $OUT_ROOT"
  section "studio make --mock"
  if ! [[ -x "$PY" ]] || ! (cd "$REPO_ROOT" && "$PY" -c 'import studio.cli' >/dev/null 2>&1); then
    skip "studio CLI not ready yet (studio.cli not importable). Another workstream is building it"
    summary; exit 0
  fi
  if ! (cd "$REPO_ROOT" && uv run --no-sync studio make --help >/dev/null 2>&1); then
    skip "'studio make' subcommand not available yet ('uv run studio make --help' failed)"
    summary; exit 0
  fi
  if (cd "$REPO_ROOT" && timed "studio make --mock" uv run --no-sync studio make --mock ${MOCK_ARGS[@]+"${MOCK_ARGS[@]}"}); then
    pass "studio make --mock completed"
  else
    fail "studio make --mock exited non-zero"
  fi
  summary
  (( N_FAIL > 0 )) && exit 1
  exit 0
fi

echo "Smoke test — stage=$STAGE  output: $OUT"

# --------------------------------------------------------------------------- LLM
if want llm; then
  section "LLM (Ollama)"
  IFS=$'\t' read -r _ _ _ LLM_TAG _ <<<"$(model_field llm)"
  if ! have ollama; then
    skip "ollama not installed"
  elif ! ollama_running; then
    skip "ollama server not running (make ollama)"
  elif ! ollama_has "$LLM_TAG"; then
    skip "model $LLM_TAG not pulled (make models STAGE=llm)"
  else
    body=$(printf '{"model":"%s","stream":false,"think":false,"prompt":"Write one punchy 12-word hook about saving money. Plain text only."}' "$LLM_TAG")
    if timed "LLM generate" curl -fsS -m 300 "${OLLAMA_BASE_URL:-http://localhost:11434}/api/generate" \
         -d "$body" -o "$OUT/llm.json"; then
      txt="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["response"].strip())' "$OUT/llm.json" 2>/dev/null)"
      if [[ -n "$txt" ]]; then
        SMOKE_TEXT="$txt"; printf '%s\n' "$txt" >"$OUT/llm.txt"
        pass "LLM ($LLM_TAG): \"$txt\""
        info "check the backend with: ollama ps   (and ollama run $LLM_TAG --verbose for tokens/s)"
      else
        fail "LLM returned no text (see $OUT/llm.json)"
      fi
    else
      fail "LLM request to Ollama failed"
    fi
  fi
fi

# --------------------------------------------------------------------------- TTS
if want tts; then
  section "TTS (Kokoro via mlx-audio)"
  IFS=$'\t' read -r _ _ _ TTS_REPO _ <<<"$(model_field tts)"
  if ! py_has mlx_audio; then
    skip "mlx-audio not installed in .venv (uv sync --extra local-ml, when disk allows)"
  elif ! hf_has "$TTS_REPO"; then
    skip "model $TTS_REPO not downloaded (make models STAGE=tts)"
  else
    rm -f "$OUT"/tts*.wav
    if (cd "$OUT" && timed "TTS" "$PY" -m mlx_audio.tts.generate --model "$TTS_REPO" \
          --text "$SMOKE_TEXT" --voice am_michael --file_prefix tts >"$OUT/tts.log" 2>&1); then
      wavs=("$OUT"/tts*.wav); wav="${wavs[0]}"
      if [[ -f "$wav" ]]; then mv -f "$wav" "$OUT/voice.wav"; pass "TTS wrote $OUT/voice.wav"
      else fail "TTS ran but wrote no wav (see $OUT/tts.log)"; fi
    else
      fail "TTS failed (see $OUT/tts.log; mlx-audio flags may have changed: python -m mlx_audio.tts.generate --help)"
    fi
  fi
fi

# --------------------------------------------------------------------------- ASR
if want asr; then
  section "ASR (parakeet-mlx word timings)"
  IFS=$'\t' read -r _ _ _ ASR_REPO _ <<<"$(model_field asr)"
  PARAKEET=""
  if have parakeet-mlx; then PARAKEET="parakeet-mlx"
  elif [[ -x "$REPO_ROOT/.venv/bin/parakeet-mlx" ]]; then PARAKEET="$REPO_ROOT/.venv/bin/parakeet-mlx"; fi
  if [[ -z "$PARAKEET" ]]; then
    skip "parakeet-mlx not installed (uv sync --extra local-ml, or uv tool install parakeet-mlx)"
  elif ! hf_has "$ASR_REPO"; then
    skip "model $ASR_REPO not downloaded (make models STAGE=asr)"
  elif [[ ! -f "$OUT/voice.wav" ]]; then
    skip "no $OUT/voice.wav to transcribe (TTS stage skipped)"
  else
    if timed "ASR" "$PARAKEET" "$OUT/voice.wav" --model "$ASR_REPO" --output-format json \
         --output-dir "$OUT" >"$OUT/asr.log" 2>&1; then
      jsons=("$OUT"/voice*.json)
      pass "ASR wrote ${jsons[0]}"
    else
      fail "ASR failed (see $OUT/asr.log)"
    fi
  fi
fi

# --------------------------------------------------------------------------- image
if want image; then
  section "Image (Z-Image-Turbo via mflux)"
  IFS=$'\t' read -r _ _ _ IMG_REPO _ <<<"$(model_field image)"
  MFLUX_BIN=""
  for c in mflux-generate-z-image-turbo "$REPO_ROOT/.venv/bin/mflux-generate-z-image-turbo"; do
    if have "$c" || [[ -x "$c" ]]; then MFLUX_BIN="$c"; break; fi
  done
  if [[ -z "$MFLUX_BIN" ]]; then
    skip "mflux not installed (uv sync --extra local-ml, or uv tool install mflux)"
  elif ! hf_has "$IMG_REPO"; then
    skip "model $IMG_REPO not downloaded (make models STAGE=image)"
  else
    if timed "image" "$MFLUX_BIN" --model "$IMG_REPO" --prompt "flat illustration of a piggy bank, bold colours" \
         --width 576 --height 1024 --steps 9 --seed 42 --output "$OUT/still.png" >"$OUT/image.log" 2>&1; then
      pass "image wrote $OUT/still.png"
    else
      fail "image generation failed (see $OUT/image.log; check flags with: $MFLUX_BIN --help)"
    fi
  fi
fi

# --------------------------------------------------------------------------- music
if want music; then
  section "Music (ACE-Step 1.5)"
  IFS=$'\t' read -r _ _ _ MUSIC_REPO _ <<<"$(model_field music)"
  ACE_DIR="${ACE_STEP_DIR:-$REPO_ROOT/vendor/ACE-Step-1.5}"
  if [[ ! -d "$ACE_DIR" ]]; then
    skip "ACE-Step code not found at $ACE_DIR (git clone https://github.com/ace-step/ACE-Step-1.5; set ACE_STEP_DIR)"
  elif ! hf_has "$MUSIC_REPO"; then
    skip "model $MUSIC_REPO not downloaded (make models STAGE=music)"
  elif [[ -z "${ACE_STEP_CMD:-}" ]]; then
    skip "set ACE_STEP_CMD to the repo's generate command writing \$OUT_WAV (CLI not pinned yet; see its README)"
  else
    if (cd "$ACE_DIR" && OUT_WAV="$OUT/music.wav" timed "music" bash -c "$ACE_STEP_CMD" >"$OUT/music.log" 2>&1) \
         && [[ -f "$OUT/music.wav" ]]; then
      pass "music wrote $OUT/music.wav"
    else
      fail "music generation failed (see $OUT/music.log)"
    fi
  fi
fi

# --------------------------------------------------------------------------- render
if want render; then
  section "Render (FFmpeg: 1080x1920, libass captions, VideoToolbox)"
  if ! have ffmpeg; then
    skip "ffmpeg not installed (make tools)"
  else
    inputs=(); maps=()
    if [[ -f "$OUT/still.png" ]]; then
      inputs+=(-loop 1 -t "$DUR" -i "$OUT/still.png"); info "visual: still.png"
    else
      inputs+=(-f lavfi -t "$DUR" -i "color=c=0x1f6feb:s=1080x1920:r=30"); info "visual: placeholder colour (image stage skipped)"
    fi
    if [[ -f "$OUT/voice.wav" ]]; then
      inputs+=(-i "$OUT/voice.wav"); info "audio: voice.wav"
    else
      inputs+=(-f lavfi -t "$DUR" -i "sine=frequency=440:sample_rate=48000"); info "audio: placeholder tone (TTS stage skipped)"
    fi
    maps=(-map 0:v -map 1:a)
    if [[ -f "$OUT/music.wav" ]]; then
      inputs+=(-i "$OUT/music.wav")
      filter_a="[1:a][2:a]amix=inputs=2:duration=first:weights=1 0.2[a]"; maps=(-map 0:v -map "[a]")
    fi
    caption="${SMOKE_TEXT//\'/}"; caption="${caption//,/\\,}"; caption="${caption:0:80}"
    cat >"$OUT/captions.ass" <<ASS
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Default,Helvetica,64,&H00FFFFFF,&H00000000,&H64000000,1,4,0,2,60,60,300

[Events]
Format: Layer, Start, End, Style, Text
Dialogue: 0,0:00:00.00,0:00:0${DUR}.00,Default,{\\q1}${caption}
ASS
    vf="scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,ass=captions.ass"
    fargs=(-filter:v "$vf")
    if [[ -n "${filter_a:-}" ]]; then fargs=(-filter_complex "[0:v]${vf}[v];${filter_a}"); maps=(-map "[v]" -map "[a]"); fi
    if (cd "$OUT" && timed "render" ffmpeg -hide_banner -loglevel error -y "${inputs[@]}" "${fargs[@]}" "${maps[@]}" \
          -t "$DUR" -c:v h264_videotoolbox -b:v 6M -pix_fmt yuv420p -c:a aac -b:a 160k \
          -movflags +faststart "$OUT/smoke.mp4" 2>"$OUT/render.log"); then
      d="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/smoke.mp4" 2>/dev/null)"
      pass "rendered $OUT/smoke.mp4 (${d:-?} s)"
    else
      fail "ffmpeg render failed (see $OUT/render.log)"
    fi
  fi
fi

summary
(( N_FAIL > 0 )) && exit 1
exit 0
