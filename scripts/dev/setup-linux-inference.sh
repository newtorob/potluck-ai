#!/bin/bash
# One-shot Linux inference setup for the Potluck contributor.
# Linux equivalent of scripts/setup-windows-inference.ps1.
#
# Auto-detects GPU vendor and picks a backend:
#   - NVIDIA (nvidia-smi works) -> llama-cpp-python with CUDA wheels
#   - AMD / Intel / no GPU      -> Ollama (handles ROCm/Vulkan/CPU itself)
#
# Run after scripts/dev/setup-linux-contributor.sh or scripts/dev/setup-linux-tailscale-contributor.sh.
# Idempotent.

set -euo pipefail

# ---------- defaults (override via flags) ------------------------------------

BACKEND="auto"          # auto | llama-cpp | ollama
MODEL_SLUG=""           # per-backend default if empty
MODEL_REPO="bartowski/Qwen2.5-3B-Instruct-GGUF"
MODEL_FILE="Qwen2.5-3B-Instruct-Q4_K_M.gguf"
N_CTX=4096
N_GPU_LAYERS=-1
OLLAMA_URL="http://127.0.0.1:11434"
NO_MODEL=0
NO_SMOKE_TEST=0

usage() {
    cat <<EOF
Usage: $0 [options]

Options:
  --backend {auto|llama-cpp|ollama}   force backend (default auto)
  --model-slug SLUG                   model identifier
  --model-repo REPO                   HF repo for llama-cpp path
  --model-file FILE                   HF filename for llama-cpp path
  --n-ctx N                           context window (default 4096)
  --n-gpu-layers N                    -1=all, 0=CPU only (default -1)
  --ollama-url URL                    Ollama daemon URL (default http://127.0.0.1:11434)
  --no-model                          skip model download/pull
  --no-smoke-test                     skip the local 5-token smoke test
  --help                              show this
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --backend)        BACKEND="$2"; shift 2 ;;
        --model-slug)     MODEL_SLUG="$2"; shift 2 ;;
        --model-repo)     MODEL_REPO="$2"; shift 2 ;;
        --model-file)     MODEL_FILE="$2"; shift 2 ;;
        --n-ctx)          N_CTX="$2"; shift 2 ;;
        --n-gpu-layers)   N_GPU_LAYERS="$2"; shift 2 ;;
        --ollama-url)     OLLAMA_URL="$2"; shift 2 ;;
        --no-model)       NO_MODEL=1; shift ;;
        --no-smoke-test)  NO_SMOKE_TEST=1; shift ;;
        --help|-h)        usage ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
done

# ---------- helpers -----------------------------------------------------------

C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YELLOW=$'\033[33m'; C_OFF=$'\033[0m'
step() { printf "%s==> %s%s\n" "$C_CYAN" "$1" "$C_OFF"; }
pass() { printf "    %s[ok]   %s%s\n" "$C_GREEN" "$1" "$C_OFF"; }
warn() { printf "    %s[warn] %s%s\n" "$C_YELLOW" "$1" "$C_OFF"; }
fail() { printf "    %s[fail] %s%s\n" "$C_RED" "$1" "$C_OFF"; exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SIDECAR_DIR="$REPO_ROOT/apps/desktop/python"
VENV_PY="$SIDECAR_DIR/.venv/bin/python"
VENV_PIP="$SIDECAR_DIR/.venv/bin/pip"
ENV_FILE="$SIDECAR_DIR/.env"

# ---------- 1. Pre-flight -----------------------------------------------------

step "Pre-flight"
[ -x "$VENV_PY" ] || fail "venv missing at $VENV_PY - run scripts/dev/setup-linux-contributor.sh first."
[ -f "$ENV_FILE" ] || fail ".env missing at $ENV_FILE - run scripts/dev/setup-linux-contributor.sh first."
pass "venv + .env present"

# ---------- 2. Detect GPU vendor + pick backend -------------------------------

step "Detecting GPU vendor"
GPU_VENDOR="none"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    GPU_VENDOR="nvidia"
elif command -v lspci >/dev/null 2>&1; then
    if lspci | grep -iE "VGA|3D" | grep -iE "AMD|ATI|Radeon" >/dev/null 2>&1; then
        GPU_VENDOR="amd"
    elif lspci | grep -iE "VGA|3D" | grep -i "Intel" >/dev/null 2>&1; then
        GPU_VENDOR="intel"
    fi
fi
pass "vendor: $GPU_VENDOR"

CHOSEN_BACKEND="$BACKEND"
if [ "$CHOSEN_BACKEND" = "auto" ]; then
    if [ "$GPU_VENDOR" = "nvidia" ]; then
        CHOSEN_BACKEND="llama-cpp"
    else
        CHOSEN_BACKEND="ollama"
    fi
    pass "auto-selected backend: $CHOSEN_BACKEND"
else
    pass "using backend: $CHOSEN_BACKEND (override)"
fi

# Per-backend default model slug.
if [ -z "$MODEL_SLUG" ]; then
    if [ "$CHOSEN_BACKEND" = "ollama" ]; then
        MODEL_SLUG="qwen2.5:3b-instruct-q4_K_M"
    else
        MODEL_SLUG="qwen-2.5-3b-instruct-q4"
    fi
fi

# ---------- 3a. llama-cpp path (NVIDIA) ---------------------------------------

install_llamacpp() {
    step "Detecting CUDA driver"
    local cuda_major="" cuda_minor=""
    if command -v nvidia-smi >/dev/null 2>&1; then
        local cuda_line
        cuda_line=$(nvidia-smi 2>/dev/null | grep -oP 'CUDA Version:\s*\K[0-9]+\.[0-9]+' | head -1 || true)
        if [ -n "$cuda_line" ]; then
            cuda_major="${cuda_line%.*}"
            cuda_minor="${cuda_line#*.}"
            pass "driver supports CUDA $cuda_major.$cuda_minor"
        fi
    fi

    step "Installing llama-cpp-python"
    local wheels=()
    if [ -n "$cuda_major" ]; then
        for cu in 126 125 124 123 122 121 118; do
            local wm wn
            wm="${cu:0:2}"; wn="${cu:2:1}"
            if [ "$wm" -lt "$cuda_major" ] || ([ "$wm" = "$cuda_major" ] && [ "$wn" -le "$cuda_minor" ]); then
                wheels+=("https://abetlen.github.io/llama-cpp-python/whl/cu$cu")
            fi
        done
    fi
    wheels+=("__pypi__")  # fallback CPU build

    local installed=0
    for w in "${wheels[@]}"; do
        if [ "$w" = "__pypi__" ]; then
            echo "    trying PyPI (CPU build)"
            "$VENV_PIP" install --upgrade --quiet llama-cpp-python 2>/dev/null && installed=1 && break
        else
            echo "    trying $w"
            "$VENV_PIP" install --upgrade --quiet llama-cpp-python --extra-index-url "$w" 2>/dev/null && installed=1 && break
        fi
        warn "  failed; trying next"
    done
    [ "$installed" = "1" ] || fail "couldn't install llama-cpp-python from any candidate."

    # Capture the actual import error if any. "Installed but won't import"
    # is usually a CUDA-runtime / glibc / arch mismatch; we want to see the
    # specific exception, not eat it. Common fix: --backend ollama to bypass.
    local import_err
    if import_err=$("$VENV_PY" -c "import llama_cpp" 2>&1); then
        pass "llama-cpp-python imports cleanly"
    else
        echo "$import_err" | sed 's/^/    /' >&2
        fail "llama-cpp-python installed but won't import (see error above). \
Easiest workaround: re-run with --backend ollama; Ollama uses CUDA too \
and installs cleanly on virtually any Linux distro."
    fi

    # Model
    local model_dir="$HOME/.potluck/models/$MODEL_SLUG"
    local model_path="$model_dir/model.gguf"
    if [ "$NO_MODEL" = "0" ] && [ ! -f "$model_path" ]; then
        step "Downloading model from Hugging Face ($MODEL_REPO / $MODEL_FILE)"
        mkdir -p "$model_dir"
        "$VENV_PIP" install --upgrade --quiet huggingface-hub 2>/dev/null || fail "couldn't install huggingface-hub"
        "$VENV_PY" - <<EOF || fail "model download failed"
from huggingface_hub import hf_hub_download
import shutil
src = hf_hub_download(repo_id="$MODEL_REPO", filename="$MODEL_FILE")
shutil.copy(src, "$model_path")
print("OK")
EOF
        pass "downloaded $(du -h "$model_path" | cut -f1) to $model_path"
    elif [ -f "$model_path" ]; then
        pass "model already at $model_path"
    fi

    # Emit .env keys via global INFERENCE_KEYS array (bash assoc-as-output via globals)
    INFERENCE_KEYS=(
        "POTLUCK_INFERENCE_ENABLED=true"
        "POTLUCK_INFERENCE_BACKEND=llama-cpp"
        "POTLUCK_MODEL_SLUG=$MODEL_SLUG"
        "POTLUCK_MODEL_PATH=$model_path"
        "POTLUCK_N_GPU_LAYERS=$N_GPU_LAYERS"
        "POTLUCK_N_CTX=$N_CTX"
    )
    SMOKE_MODEL_PATH="$model_path"  # for smoke step
}

# ---------- 3b. Ollama path (AMD / Intel / no GPU) ----------------------------

install_ollama() {
    step "Checking Ollama install"
    if ! command -v ollama >/dev/null 2>&1; then
        step "Installing Ollama (curl https://ollama.com/install.sh | sh)"
        if ! command -v curl >/dev/null 2>&1; then
            fail "curl missing - apt install curl, then re-run."
        fi
        if ! command -v sudo >/dev/null 2>&1; then
            fail "sudo missing and ollama install needs root. Install Ollama manually per https://ollama.com/download/linux"
        fi
        warn "this prompts for sudo to install systemd unit + binary"
        curl -fsSL https://ollama.com/install.sh | sh || fail "ollama install failed"
        if ! command -v ollama >/dev/null 2>&1; then
            fail "ollama installed but not on PATH; open a new shell and re-run"
        fi
        pass "Ollama installed"
    else
        pass "Ollama already on PATH"
    fi

    step "Waiting for Ollama daemon at $OLLAMA_URL"
    local ready=0
    for i in $(seq 1 30); do
        if curl -fsS --max-time 2 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
            ready=1; break
        fi
        sleep 1
    done
    if [ "$ready" = "0" ]; then
        # Linux: install script registers a systemd unit, but on some
        # distros (no-systemd, minimal containers) we need to start manually.
        warn "daemon not responding; trying 'sudo systemctl start ollama'"
        sudo systemctl start ollama 2>/dev/null || true
        for i in $(seq 1 15); do
            if curl -fsS --max-time 2 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
                ready=1; break
            fi
            sleep 1
        done
    fi
    [ "$ready" = "1" ] || fail "Ollama daemon not reachable at $OLLAMA_URL. Try 'ollama serve' in a separate terminal, then re-run."
    pass "Ollama daemon is up"

    if [ "$NO_MODEL" = "0" ]; then
        step "Pulling model: ollama pull $MODEL_SLUG"
        echo "    this downloads ~2 GB on first run"
        ollama pull "$MODEL_SLUG" || fail "ollama pull failed. Check the slug at https://ollama.com/library"
        pass "model pulled"
    fi

    INFERENCE_KEYS=(
        "POTLUCK_INFERENCE_ENABLED=true"
        "POTLUCK_INFERENCE_BACKEND=ollama"
        "POTLUCK_MODEL_SLUG=$MODEL_SLUG"
        "POTLUCK_OLLAMA_URL=$OLLAMA_URL"
        "POTLUCK_N_CTX=$N_CTX"
    )
    SMOKE_MODEL_PATH=""
}

# ---------- run the chosen path ----------------------------------------------

INFERENCE_KEYS=()
SMOKE_MODEL_PATH=""
if [ "$CHOSEN_BACKEND" = "llama-cpp" ]; then
    install_llamacpp
else
    install_ollama
fi

# ---------- 4. Update .env ----------------------------------------------------

step "Updating $ENV_FILE"
INFERENCE_KEY_NAMES=(
    "POTLUCK_INFERENCE_ENABLED"
    "POTLUCK_INFERENCE_BACKEND"
    "POTLUCK_MODEL_SLUG"
    "POTLUCK_MODEL_PATH"
    "POTLUCK_N_GPU_LAYERS"
    "POTLUCK_N_CTX"
    "POTLUCK_OLLAMA_URL"
)

# Build a regex like '^(POTLUCK_INFERENCE_ENABLED|POTLUCK_..)='
strip_re="^("
for k in "${INFERENCE_KEY_NAMES[@]}"; do strip_re="$strip_re$k|"; done
strip_re="${strip_re%|})="

tmp_env="$(mktemp)"
grep -vE "$strip_re" "$ENV_FILE" > "$tmp_env" || true
{
    echo ""
    echo "# Inference (added by scripts/dev/setup-linux-inference.sh)"
    echo "# Backend: $CHOSEN_BACKEND (auto-detected from GPU vendor: $GPU_VENDOR)"
    for kv in "${INFERENCE_KEYS[@]}"; do echo "$kv"; done
} >> "$tmp_env"
mv "$tmp_env" "$ENV_FILE"
pass "wrote .env (${#INFERENCE_KEYS[@]} keys)"

# ---------- 5. Smoke test -----------------------------------------------------

if [ "$NO_SMOKE_TEST" = "0" ]; then
    step "Smoke-testing inference (5-token generation)"
    if [ "$CHOSEN_BACKEND" = "llama-cpp" ]; then
        echo "    cold-loading model, takes 10-60s..."
        "$VENV_PY" - <<EOF || fail "llama-cpp smoke test failed"
import time
from llama_cpp import Llama
t0 = time.monotonic()
llm = Llama(model_path="$SMOKE_MODEL_PATH", n_ctx=$N_CTX, n_gpu_layers=$N_GPU_LAYERS, verbose=False)
load_s = time.monotonic() - t0
t1 = time.monotonic()
out = llm.create_completion(prompt="Hi", max_tokens=5, temperature=0.1)
gen_s = time.monotonic() - t1
print(f"OK load_s={load_s:.1f} gen_s={gen_s:.2f}")
EOF
        pass "llama-cpp smoke test passed"
    else
        # Ollama smoke via the API
        body=$(printf '{"model":"%s","prompt":"Hi","stream":false,"options":{"num_predict":5,"temperature":0.1}}' "$MODEL_SLUG")
        local_t0=$(date +%s)
        if ! curl -fsS --max-time 120 "$OLLAMA_URL/api/generate" -H "Content-Type: application/json" -d "$body" >/dev/null; then
            fail "Ollama smoke test failed"
        fi
        elapsed=$(( $(date +%s) - local_t0 ))
        pass "Ollama smoke test passed (${elapsed}s)"
    fi
else
    warn "skipping smoke test"
fi

# ---------- 6. Done -----------------------------------------------------------

cat <<EOF

${C_CYAN}==> Inference setup complete${C_OFF}

Backend:    $CHOSEN_BACKEND
Model slug: $MODEL_SLUG
Context:    $N_CTX tokens

To activate: restart the contributor sidecar.
   systemctl --user restart potluck-contributor
   journalctl --user -u potluck-contributor -f      # watch the log

To verify from the coordinator machine after restart:
   curl -X POST http://<mac-ip>:8400/jobs -H 'Content-Type: application/json' \\
     -d '{"prompt":"Hello","model_slug":"$MODEL_SLUG","sampling":{"max_tokens":20}}'

If something looks wrong, re-run with --no-model --no-smoke-test after fixing.

EOF
