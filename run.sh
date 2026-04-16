#!/usr/bin/env bash
# Story / History Shorts — single-command entry point.
# Bootstraps a self-contained environment under this repo, runs the pipeline,
# and (optionally) tears everything down. Nothing is installed outside this folder.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

VENV_DIR="$REPO_ROOT/.venv"
VENDOR_DIR="$REPO_ROOT/vendor"
WHISPER_DIR="$VENDOR_DIR/whisper.cpp"
KOKORO_DIR="$VENDOR_DIR/kokoro"
HF_CACHE_DIR="$VENDOR_DIR/hf_cache"
WORKSPACE_DIR="$REPO_ROOT/workspace"
PY_BIN="${PY_BIN:-python3.12}"

export HF_HOME="$HF_CACHE_DIR"
export TRANSFORMERS_CACHE="$HF_CACHE_DIR"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1

# Forward secrets from .env if present (don't fail if missing)
if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env"
  set +a
fi

log() { printf "\033[1;36m[run.sh]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[run.sh]\033[0m %s\n" "$*" >&2; }
die() { printf "\033[1;31m[run.sh]\033[0m %s\n" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
YTShorts-Automater — story/history Shorts pipeline

Usage:
  ./run.sh                          Bootstrap + run with today's "on-this-day" topic
  ./run.sh --topic "Apollo 13"      Run for a specific topic
  ./run.sh --dry-run                Run every stage except YouTube upload
  ./run.sh --bootstrap-only         Just set up venv + whisper.cpp + Kokoro models
  ./run.sh --cleanup                Remove .venv, vendor/, workspace/ (keeps source + logs)
  ./run.sh --help                   Show this message

Required keys (set in .env file at repo root, OR in config/settings.yaml):
  KLING_ACCESS_KEY                  Kling text-to-video (free, app.klingai.com/global/dev)
  KLING_SECRET_KEY                  Kling text-to-video (free, app.klingai.com/global/dev)
  PIXABAY_API_KEY                   OPTIONAL — background music (free, pixabay.com/api/docs/)

YouTube uploads also need config/client_secrets.json (Desktop OAuth2 client).
EOF
}

cleanup() {
  log "Tearing down local installs (leaves source + logs intact)"
  rm -rf "$VENV_DIR" "$VENDOR_DIR" "$WORKSPACE_DIR"
  mkdir -p "$WORKSPACE_DIR"/{scenes,clips,audio,output}
  log "Done. Source tree + logs/ preserved."
}

check_system_deps() {
  command -v "$PY_BIN" >/dev/null 2>&1 || die "Python 3.12 not found. brew install python@3.12"
  command -v ffmpeg  >/dev/null 2>&1 || die "ffmpeg not found. brew install ffmpeg-full"
  command -v git     >/dev/null 2>&1 || die "git not found."
  command -v make    >/dev/null 2>&1 || die "make not found (xcode-select --install)."
  command -v cmake   >/dev/null 2>&1 || die "cmake not found. brew install cmake (for whisper.cpp)."
  command -v curl    >/dev/null 2>&1 || die "curl not found."
  if ! ffmpeg -hide_banner -filters 2>/dev/null | grep -qE '^ .. (ass|subtitles) '; then
    warn "ffmpeg has no libass — subtitles will be skipped. brew uninstall --ignore-dependencies ffmpeg && brew install ffmpeg-full"
  fi
  if ! command -v ollama >/dev/null 2>&1; then
    warn "ollama not in PATH — pipeline will use rule-based script fallback. Install from https://ollama.com"
  fi
}

ensure_venv() {
  if [ ! -d "$VENV_DIR" ]; then
    log "Creating venv at $VENV_DIR with $PY_BIN"
    "$PY_BIN" -m venv "$VENV_DIR"
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  python -m pip install --upgrade pip wheel setuptools >/dev/null
  local stamp="$VENV_DIR/.requirements.stamp"
  if [ ! -f "$stamp" ] || [ "$REPO_ROOT/requirements.txt" -nt "$stamp" ]; then
    log "Installing Python dependencies (one-time cost)"
    pip install -r "$REPO_ROOT/requirements.txt"
    touch "$stamp"
  fi
}

ensure_whisper_cpp() {
  mkdir -p "$VENDOR_DIR"
  if [ ! -d "$WHISPER_DIR" ]; then
    log "Cloning whisper.cpp into $WHISPER_DIR"
    git clone --depth=1 https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
  fi
  if [ ! -x "$WHISPER_DIR/main" ] && [ ! -x "$WHISPER_DIR/build/bin/whisper-cli" ]; then
    log "Building whisper.cpp with Metal acceleration (CMake)"
    ( cd "$WHISPER_DIR" && cmake -B build -DGGML_METAL=1 -DCMAKE_BUILD_TYPE=Release && cmake --build build --config Release -j )
  fi
  if [ ! -f "$WHISPER_DIR/models/ggml-base.en.bin" ]; then
    log "Downloading whisper.cpp base.en model (~150MB)"
    ( cd "$WHISPER_DIR" && bash ./models/download-ggml-model.sh base.en )
  fi
}

ensure_kokoro_models() {
  mkdir -p "$KOKORO_DIR"
  local model="$KOKORO_DIR/kokoro-v1.0.onnx"
  local voices="$KOKORO_DIR/voices-v1.0.bin"
  if [ ! -f "$model" ]; then
    log "Downloading Kokoro TTS model (~330MB)"
    curl -L --fail --retry 3 -o "$model" \
      "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
  fi
  if [ ! -f "$voices" ]; then
    log "Downloading Kokoro voice pack (~26MB)"
    curl -L --fail --retry 3 -o "$voices" \
      "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
  fi
}

ensure_ollama_running() {
  command -v ollama >/dev/null 2>&1 || return 0
  if curl -sf "http://localhost:11434/api/tags" >/dev/null 2>&1; then return 0; fi
  warn "Ollama not responding on localhost:11434 — pipeline will use rule-based script fallback."
  warn "Start it: 'ollama serve &' in another terminal."
}

run_pipeline() {
  log "Launching pipeline"
  python -m pipeline.orchestrator "$@"
}

# ---------- arg parsing ----------
MODE="run"
PASSTHROUGH=()
while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h)         usage; exit 0 ;;
    --cleanup)         MODE="cleanup"; shift ;;
    --bootstrap-only)  MODE="bootstrap"; shift ;;
    --)                shift; PASSTHROUGH+=("$@"); break ;;
    *)                 PASSTHROUGH+=("$1"); shift ;;
  esac
done

if [ "$MODE" = "cleanup" ]; then
  cleanup
  exit 0
fi

check_system_deps
ensure_venv
ensure_whisper_cpp
ensure_kokoro_models
ensure_ollama_running

if [ "$MODE" = "bootstrap" ]; then
  log "Bootstrap complete. Run './run.sh --topic \"...\"' to make a Short."
  exit 0
fi

run_pipeline "${PASSTHROUGH[@]:-}"
