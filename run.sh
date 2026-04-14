#!/usr/bin/env bash
# Cricket YouTube Shorts — single-command entry point.
# Bootstraps a self-contained environment under this repo, runs the pipeline,
# and (optionally) tears everything down. Nothing is installed outside this folder.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

VENV_DIR="$REPO_ROOT/.venv"
VENDOR_DIR="$REPO_ROOT/vendor"
WHISPER_DIR="$VENDOR_DIR/whisper.cpp"
HF_CACHE_DIR="$VENDOR_DIR/hf_cache"
WORKSPACE_DIR="$REPO_ROOT/workspace"
PY_BIN="${PY_BIN:-python3.12}"

export HF_HOME="$HF_CACHE_DIR"
export TRANSFORMERS_CACHE="$HF_CACHE_DIR"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1

log() { printf "\033[1;36m[run.sh]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[run.sh]\033[0m %s\n" "$*" >&2; }
die() { printf "\033[1;31m[run.sh]\033[0m %s\n" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Cricket YouTube Shorts — run.sh

Usage:
  ./run.sh                         Bootstrap + run once (auto-picks latest completed match)
  ./run.sh --match-id <id>         Run for a specific cricinfo match id
  ./run.sh --watch                 Run the polling trigger loop
  ./run.sh --dry-run               Run every stage except YouTube upload
  ./run.sh --bootstrap-only        Just set up venv + whisper.cpp, do not run
  ./run.sh --cleanup               Remove .venv, vendor/, workspace/ (keeps source + logs)
  ./run.sh --help                  Show this message

Any flags after "--" are forwarded verbatim to pipeline/orchestrator.py.
EOF
}

cleanup() {
  log "Tearing down local installs (leaves source + logs intact)"
  rm -rf "$VENV_DIR"
  rm -rf "$VENDOR_DIR"
  rm -rf "$WORKSPACE_DIR"
  mkdir -p "$WORKSPACE_DIR"/{downloads,clips,audio,output}
  log "Done. Source tree + logs/ preserved."
}

check_system_deps() {
  command -v "$PY_BIN" >/dev/null 2>&1 || die "Python 3.12 not found. Install with 'brew install python@3.12' or set PY_BIN=/path/to/python3.12."
  command -v ffmpeg  >/dev/null 2>&1 || die "ffmpeg not found. Install with 'brew install ffmpeg'."
  command -v git     >/dev/null 2>&1 || die "git not found."
  command -v make    >/dev/null 2>&1 || die "make not found (install Xcode Command Line Tools: xcode-select --install)."
  command -v cmake   >/dev/null 2>&1 || die "cmake not found. Install with 'brew install cmake' (required by whisper.cpp)."
  if ! command -v ollama >/dev/null 2>&1; then
    warn "ollama not found in PATH. Install from https://ollama.com — the pipeline will fall back to rule-based decisions without it."
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
    log "Installing Python dependencies (this is a one-time cost)"
    pip install -r "$REPO_ROOT/requirements.txt"
    # MusicGen-MLX is best-effort. There's no published PyPI package with that exact name,
    # so we try two common GitHub sources and continue quietly on failure — the pipeline
    # falls back to a silent background track.
    log "Attempting MusicGen-MLX install (best-effort, will fall back to silent music)"
    pip install "musicgen-mlx @ git+https://github.com/andrade0/musicgen-mlx" 2>/dev/null \
      || pip install audiocraft 2>/dev/null \
      || warn "No MusicGen backend installed — Shorts will be produced without background music."
    touch "$stamp"
  fi
}

ensure_whisper_cpp() {
  mkdir -p "$VENDOR_DIR"
  if [ ! -d "$WHISPER_DIR" ]; then
    log "Cloning whisper.cpp into $WHISPER_DIR"
    git clone --depth=1 https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
  fi
  if [ ! -x "$WHISPER_DIR/main" ] \
     && [ ! -x "$WHISPER_DIR/build/bin/whisper-cli" ] \
     && [ ! -x "$WHISPER_DIR/build/bin/main" ]; then
    log "Building whisper.cpp with Metal acceleration (CMake)"
    (
      cd "$WHISPER_DIR"
      cmake -B build -DGGML_METAL=1 -DCMAKE_BUILD_TYPE=Release
      cmake --build build --config Release -j
    )
  fi
  local model_path="$WHISPER_DIR/models/ggml-base.en.bin"
  if [ ! -f "$model_path" ]; then
    log "Downloading whisper.cpp base.en model (~150MB — large-v3 upgrade: ./vendor/whisper.cpp/models/download-ggml-model.sh large-v3)"
    ( cd "$WHISPER_DIR" && bash ./models/download-ggml-model.sh base.en )
  fi
}

ensure_ollama_running() {
  if ! command -v ollama >/dev/null 2>&1; then return 0; fi
  if curl -sf "http://localhost:11434/api/tags" >/dev/null 2>&1; then return 0; fi
  warn "Ollama not responding at localhost:11434. Start it with 'ollama serve &' in another terminal, or the pipeline will use rule-based fallback."
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
    --help|-h)          usage; exit 0 ;;
    --cleanup)          MODE="cleanup"; shift ;;
    --bootstrap-only)   MODE="bootstrap"; shift ;;
    --)                 shift; PASSTHROUGH+=("$@"); break ;;
    *)                  PASSTHROUGH+=("$1"); shift ;;
  esac
done

if [ "$MODE" = "cleanup" ]; then
  cleanup
  exit 0
fi

check_system_deps
ensure_venv
ensure_whisper_cpp
ensure_ollama_running

if [ "$MODE" = "bootstrap" ]; then
  log "Bootstrap complete. Run './run.sh' to execute the pipeline."
  exit 0
fi

run_pipeline "${PASSTHROUGH[@]:-}"
