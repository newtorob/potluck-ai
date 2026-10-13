#!/usr/bin/env bash
#
# setup-household-contributor.sh
#
# One-shot Linux setup for a Potluck **household** contributor. This is the
# script for "I want my Linux box to serve inference for my own laptop over
# my private WireGuard household mesh." It is not for joining the open
# cooperative network - see setup-linux-contributor.sh for that.
#
# What it does in order:
#   1. Detects distro (Ubuntu/Debian, Fedora/RHEL, Arch) and installs the
#      build tools, Python 3.11+, and Go 1.22+ needed.
#   2. Builds the Go mesh sidecar (apps/desktop/go/potluck-mesh).
#   3. Sets up the Python virtualenv at apps/desktop/python/.venv and
#      installs the sidecar's pip requirements.
#   4. If running on a Linux desktop (a graphical session is detected),
#      also installs Node + pnpm, builds the Electron desktop app, and
#      drops a "Potluck" entry in your application launcher. On a headless
#      server (TTY-only / SSH session, no $DISPLAY), skips the GUI build
#      and stays sidecar-only. Override with --gui / --headless.
#   5. Starts the sidecars in the background under nohup, with the mesh
#      sidecar running under sudo (needed for TUN creation).
#   6. GUI mode: launches the Electron app - pair + manage via the in-app
#      Household view.
#      Headless mode: walks you through pairing, loading a model, and
#      turning on contributor mode entirely from the CLI.
#
# Supported distros:
#   - Ubuntu 22.04 / 24.04, Debian 12+
#   - Fedora 38+, RHEL 9+, Rocky 9, AlmaLinux 9
#   - Arch / Manjaro
#
# Modes:
#   $ ./setup-household.sh                 # full setup + run (auto-detects GUI)
#   $ ./setup-household.sh --setup-only    # install + build only
#   $ ./setup-household.sh --run-only      # start sidecars + pair
#   $ ./setup-household.sh --stop          # stop running processes
#   $ ./setup-household.sh --status        # show what's running
#   $ ./setup-household.sh --headless      # force CLI-only, no Electron build
#   $ ./setup-household.sh --gui           # force GUI build even with no DISPLAY
#
# Re-running is safe - every step skips work that's already done.
#
# Privacy note:
#   The Python sidecar binds to 0.0.0.0 so household peers can dial it over
#   the mesh. A path-based access middleware restricts non-loopback callers
#   to /peer/* + /health - your memory, tasks, and chat history are still
#   only reachable from this machine.

set -euo pipefail

# ----------------------------------------------------------------------------
# Constants + paths
# ----------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MESH_DIR="$REPO_ROOT/apps/desktop/go/potluck-mesh"
PY_DIR="$REPO_ROOT/apps/desktop/python"
VENV_DIR="$PY_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"

STATE_DIR="$HOME/.potluck"
LOG_DIR="$STATE_DIR/logs"
PID_DIR="$STATE_DIR/run"
GO_DIR="$STATE_DIR/go"

MESH_BIN="$MESH_DIR/dist/potluck-mesh"
MESH_LOG="$LOG_DIR/mesh.log"
MESH_PID="$PID_DIR/mesh.pid"
PY_LOG="$LOG_DIR/python.log"
PY_PID="$PID_DIR/python.pid"
APP_LOG="$LOG_DIR/app.log"
APP_PID="$PID_DIR/app.pid"
FRONTEND_DIR="$REPO_ROOT/apps/desktop/frontend"
ELECTRON_DIR="$REPO_ROOT/apps/desktop/electron"

PYTHON_PORT="${POTLUCK_PYTHON_PORT:-8321}"
GO_MIN_VERSION="1.22"
PY_MIN_VERSION="3.11"

# Detect mesh socket path the way the Go sidecar does (localapi/listener_unix.go).
if [ -n "${XDG_RUNTIME_DIR:-}" ]; then
    MESH_SOCKET="$XDG_RUNTIME_DIR/potluck-mesh.sock"
else
    MESH_SOCKET="/run/user/$(id -u)/potluck-mesh.sock"
fi

mkdir -p "$LOG_DIR" "$PID_DIR"

# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------

if [ -t 1 ]; then
    C_CYAN=$'\033[36m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_RED=$'\033[31m'
    C_DIM=$'\033[2m'
    C_OFF=$'\033[0m'
else
    C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_OFF=""
fi

step() { printf "\n%s==> %s%s\n" "$C_CYAN" "$1" "$C_OFF"; }
ok()   { printf "    %s[ok]%s %s\n"   "$C_GREEN"  "$C_OFF" "$1"; }
info() { printf "    %s%s%s\n"        "$C_DIM"    "$1" "$C_OFF"; }
warn() { printf "    %s[warn]%s %s\n" "$C_YELLOW" "$C_OFF" "$1"; }
fail() { printf "    %s[fail]%s %s\n" "$C_RED"    "$C_OFF" "$1" >&2; exit 1; }

ask() {
    # ask "Prompt text" [default]  ->  echoes response on stdout
    local prompt="$1"
    local default="${2:-}"
    local reply
    if [ -n "$default" ]; then
        read -r -p "    $prompt [$default]: " reply
        echo "${reply:-$default}"
    else
        read -r -p "    $prompt: " reply
        echo "$reply"
    fi
}

# ----------------------------------------------------------------------------
# Distro detection + package install
# ----------------------------------------------------------------------------

DISTRO_FAMILY=""  # "debian" | "fedora" | "arch" | "unknown"

detect_distro() {
    if [ ! -r /etc/os-release ]; then
        DISTRO_FAMILY="unknown"
        return
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    local id="${ID:-}"
    local idlike="${ID_LIKE:-}"

    case "$id $idlike" in
        *debian*|*ubuntu*)        DISTRO_FAMILY="debian" ;;
        *fedora*|*rhel*|*centos*) DISTRO_FAMILY="fedora" ;;
        *arch*|*manjaro*)         DISTRO_FAMILY="arch" ;;
        *)                        DISTRO_FAMILY="unknown" ;;
    esac
}

install_system_deps() {
    step "Installing system dependencies"
    case "$DISTRO_FAMILY" in
        debian)
            info "detected Debian/Ubuntu family - using apt"
            sudo apt-get update -qq
            sudo apt-get install -y --no-install-recommends \
                git curl ca-certificates \
                build-essential pkg-config \
                python3 python3-venv python3-pip python3-dev \
                wireguard-tools
            ok "apt packages installed"
            ;;
        fedora)
            info "detected Fedora/RHEL family - using dnf"
            sudo dnf install -y \
                git curl ca-certificates \
                gcc gcc-c++ make pkgconf-pkg-config \
                python3 python3-virtualenv python3-pip python3-devel \
                wireguard-tools
            ok "dnf packages installed"
            ;;
        arch)
            info "detected Arch family - using pacman"
            sudo pacman -Sy --noconfirm --needed \
                git curl base-devel \
                python python-virtualenv python-pip \
                wireguard-tools
            ok "pacman packages installed"
            ;;
        *)
            warn "unknown distro - install these by hand and re-run:"
            warn "  git, curl, a C toolchain, pkg-config, Python 3.11+, wireguard-tools"
            fail "unsupported distro; see /etc/os-release"
            ;;
    esac
}

# ----------------------------------------------------------------------------
# Go installation (independent of distro package versions)
# ----------------------------------------------------------------------------

go_version_ok() {
    # Returns 0 if go is on PATH and >= GO_MIN_VERSION.
    local go_bin="${1:-go}"
    local raw
    raw=$("$go_bin" version 2>/dev/null | awk '{print $3}') || return 1
    # Strip leading "go"
    local ver="${raw#go}"
    # Major.minor only
    local major="${ver%%.*}"
    local rest="${ver#*.}"
    local minor="${rest%%.*}"
    [ -n "$major" ] && [ -n "$minor" ] || return 1
    if [ "$major" -gt 1 ]; then return 0; fi
    if [ "$major" -eq 1 ] && [ "$minor" -ge 22 ]; then return 0; fi
    return 1
}

install_go() {
    step "Checking Go $GO_MIN_VERSION+"

    if command -v go >/dev/null && go_version_ok go; then
        ok "system go is $(go version | awk '{print $3}')"
        return
    fi

    if [ -x "$GO_DIR/bin/go" ] && go_version_ok "$GO_DIR/bin/go"; then
        ok "using pre-installed go at $GO_DIR/bin/go"
        export PATH="$GO_DIR/bin:$PATH"
        return
    fi

    info "no acceptable go found; downloading official tarball into $GO_DIR"

    # Pick the right arch
    local arch
    case "$(uname -m)" in
        x86_64)        arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        *) fail "unsupported CPU arch: $(uname -m)" ;;
    esac

    local go_ver="1.22.10"
    local tarball="go${go_ver}.linux-${arch}.tar.gz"
    local url="https://go.dev/dl/${tarball}"

    mkdir -p "$GO_DIR"
    info "downloading $url"
    curl -fsSL "$url" -o "/tmp/$tarball"
    tar -C "$STATE_DIR" -xzf "/tmp/$tarball"
    rm "/tmp/$tarball"
    ok "go $go_ver installed at $GO_DIR/bin/go"
    export PATH="$GO_DIR/bin:$PATH"
}

# ----------------------------------------------------------------------------
# Python version check
# ----------------------------------------------------------------------------

python_version_ok() {
    local py="$1"
    "$py" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null
}

pick_python() {
    step "Picking Python $PY_MIN_VERSION+"
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$candidate" >/dev/null && python_version_ok "$candidate"; then
            PYTHON_BIN="$candidate"
            ok "using $candidate ($("$candidate" --version 2>&1))"
            return
        fi
    done
    case "$DISTRO_FAMILY" in
        debian)
            fail "Python 3.11+ not found. On Ubuntu 22.04: 'sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt install -y python3.11 python3.11-venv python3.11-dev'"
            ;;
        fedora)
            fail "Python 3.11+ not found. 'sudo dnf install -y python3.11'"
            ;;
        arch)
            fail "Python 3.11+ not found. 'sudo pacman -S python'"
            ;;
        *)
            fail "Python 3.11+ not found"
            ;;
    esac
}

# ----------------------------------------------------------------------------
# Build the Go mesh sidecar
# ----------------------------------------------------------------------------

build_mesh_sidecar() {
    step "Building potluck-mesh"
    (cd "$MESH_DIR" && make build >/dev/null)
    [ -x "$MESH_BIN" ] || fail "mesh binary missing at $MESH_BIN after build"
    ok "built $MESH_BIN"
}

# ----------------------------------------------------------------------------
# Set up Python venv + install deps
# ----------------------------------------------------------------------------

setup_python_venv() {
    step "Setting up Python venv at $VENV_DIR"
    if [ ! -d "$VENV_DIR" ]; then
        "$PYTHON_BIN" -m venv "$VENV_DIR"
        ok "venv created"
    else
        ok "venv already exists"
    fi
    info "upgrading pip"
    "$VENV_PY" -m pip install --quiet --upgrade pip
    info "installing requirements.txt (this can take a minute)"
    "$VENV_PY" -m pip install --quiet -r "$PY_DIR/requirements.txt"
    info "installing potluck_local in editable mode"
    "$VENV_PY" -m pip install --quiet -e "$PY_DIR"
    ok "Python sidecar deps installed"
}

# ----------------------------------------------------------------------------
# Node + pnpm install (GUI mode only)
# ----------------------------------------------------------------------------
#
# Distro package versions of Node are commonly too old (Ubuntu 22.04 ships
# Node 12). We use the NodeSource setup script on Debian-family, distro pkg
# on Fedora/Arch (both ship modern Node these days).

node_version_ok() {
    command -v node >/dev/null || return 1
    node -e 'process.exit(parseInt(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' 2>/dev/null
}

install_node_pnpm() {
    step "Installing Node.js + pnpm (for the Electron desktop app)"

    if node_version_ok; then
        ok "system node is $(node --version)"
    else
        case "$DISTRO_FAMILY" in
            debian)
                info "installing NodeSource setup for Node 20.x (Ubuntu/Debian)"
                curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null
                sudo apt-get install -y nodejs >/dev/null
                ;;
            fedora)
                info "installing nodejs via dnf"
                sudo dnf install -y nodejs npm >/dev/null
                ;;
            arch)
                info "installing nodejs + npm via pacman"
                sudo pacman -Sy --noconfirm --needed nodejs npm >/dev/null
                ;;
            *)
                fail "automatic Node install not supported on this distro. Install Node 18+ manually (via nvm: 'curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash' then 'nvm install 20'), then re-run."
                ;;
        esac
        node_version_ok || fail "node still missing or too old after install. Open a new shell so PATH refreshes, then re-run --setup-only."
        ok "node $(node --version)"
    fi

    if ! command -v pnpm >/dev/null; then
        info "installing pnpm globally via npm"
        sudo npm install -g pnpm >/dev/null 2>&1
        command -v pnpm >/dev/null || fail "pnpm not on PATH after install. Open a new shell, re-run --setup-only."
    fi
    ok "pnpm $(pnpm --version)"
}

# ----------------------------------------------------------------------------
# Build the Electron desktop app (GUI mode only)
# ----------------------------------------------------------------------------

build_electron_app() {
    step "Building Electron desktop app (~5 minutes the first time)"
    info "installing frontend deps + building production bundle"
    (cd "$FRONTEND_DIR" && pnpm install --silent && pnpm build >/dev/null) \
        || fail "frontend build failed (see $FRONTEND_DIR for details)"
    info "installing Electron deps (downloads Electron runtime ~150MB)"
    (cd "$ELECTRON_DIR" && pnpm install --silent) \
        || fail "electron deps install failed"
    ok "Electron app ready"
}

# ----------------------------------------------------------------------------
# Desktop entry (.desktop file for app launchers)
# ----------------------------------------------------------------------------

create_desktop_entry() {
    step "Creating Desktop entry for app launcher"
    local apps_dir="$HOME/.local/share/applications"
    mkdir -p "$apps_dir"

    local entry="$apps_dir/potluck.desktop"
    local icon="$ELECTRON_DIR/icons/icon.png"
    local self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

    cat > "$entry" <<EOF
[Desktop Entry]
Name=Potluck
GenericName=Cooperative AI
Comment=Member-owned AI for your household
Exec=$self --run-only
Icon=$icon
Terminal=true
Type=Application
Categories=Network;Utility;
EOF
    # Some desktops want the file to be executable. Harmless when not required.
    chmod +x "$entry" 2>/dev/null || true

    # Refresh the desktop database so file managers / launchers pick it up
    # immediately. Quiet failure - newer desktops watch the dir directly.
    if command -v update-desktop-database >/dev/null; then
        update-desktop-database "$apps_dir" 2>/dev/null || true
    fi

    ok "desktop entry at $entry"
    info "find 'Potluck' in your application launcher (Activities / Apps menu)"
}

# ----------------------------------------------------------------------------
# Sidecar lifecycle helpers
# ----------------------------------------------------------------------------

is_running() {
    # is_running <pidfile> -> 0 if a live process is recorded
    local pidfile="$1"
    [ -f "$pidfile" ] || return 1
    local pid
    pid=$(cat "$pidfile" 2>/dev/null || echo "")
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

start_mesh_sidecar() {
    step "Starting mesh sidecar (needs sudo for TUN creation)"

    if is_running "$MESH_PID"; then
        ok "mesh sidecar already running (pid $(cat "$MESH_PID"))"
        return
    fi

    local coord_url
    coord_url="$(ask "Laptop coordinator URL" "${POTLUCK_COORDINATOR_URL:-http://192.168.1.10:8400}")"

    info "verifying coordinator is reachable at $coord_url"
    if ! curl -fsS --max-time 5 "$coord_url/health" >/dev/null 2>&1; then
        warn "coordinator did not respond at $coord_url/health"
        warn "  - on the laptop, run: make up (and verify with: curl http://localhost:8400/health)"
        warn "  - check the laptop's LAN IP: ipconfig getifaddr en0 (Mac) / ip -4 addr show (Linux)"
        warn "  - firewall on the laptop must allow inbound TCP/8400 from this machine"
        fail "fix coordinator reachability and re-run with --run-only"
    fi
    ok "coordinator reachable"

    # DERP relay URL - the fallback path when direct UDP punch fails (hairpin
    # NAT, symmetric NAT, restrictive firewalls). Without it, nodes can be
    # roster-aware but unable to exchange traffic. Defaults to the production
    # relay (services/derp, tcp+tls per internal/derp/client.go); it is not
    # derived from the coordinator URL, since a self-hosted/LAN coordinator
    # does not imply a self-hosted relay. Override with $POTLUCK_DERP_URL,
    # e.g. tcp+insecure://<lan-host>:8443 for LAN-only dev against a local
    # services/derp run without TLS.
    local derp_url
    derp_url="${POTLUCK_DERP_URL:-tcp+tls://derp.trypotluck.ai:443}"
    info "derp relay URL: $derp_url"

    # Forward the user's sudo credentials so the chown-to-SUDO_UID logic in
    # listener_unix.go puts the IPC socket under our regular UID.
    info "starting mesh sidecar in background (log: $MESH_LOG)"
    sudo -E env SUDO_UID="$(id -u)" SUDO_GID="$(id -g)" \
        nohup "$MESH_BIN" --coordinator-url "$coord_url" --derp-url "$derp_url" \
        >"$MESH_LOG" 2>&1 &
    local pid=$!
    echo "$pid" >"$MESH_PID"

    # Wait up to 10s for the IPC socket to appear.
    local i=0
    while [ $i -lt 20 ]; do
        if [ -S "$MESH_SOCKET" ]; then
            ok "mesh sidecar up (pid $pid, socket $MESH_SOCKET)"
            return
        fi
        sleep 0.5
        i=$((i + 1))
    done
    warn "mesh sidecar didn't open the IPC socket in 10s - recent log:"
    tail -n 20 "$MESH_LOG" | sed 's/^/      | /' >&2
    fail "mesh sidecar failed to start"
}

resolve_cuda_lib_path() {
    # If the installed llama-cpp-python wheel was built with CUDA support,
    # libllama.so depends on libcudart.so.12 + libcublas.so.12 + libcublasLt.so.12.
    # These ship with the CUDA Toolkit but not with the bare NVIDIA driver,
    # and most contributor machines have the driver only.
    #
    # Workaround: if Ollama is installed, its CUDA-12 bundle at
    # /usr/local/lib/ollama/cuda_v12/ already has all three. Use it.
    # Returns the path to prepend to LD_LIBRARY_PATH, or empty string if
    # not needed / not available.
    local llama_lib="$PY_DIR/.venv/lib/python3.11/site-packages/llama_cpp/lib/libllama.so"
    [ ! -f "$llama_lib" ] && { echo ""; return; }

    # Does libllama.so actually link against libcudart.so.12?
    if ! ldd "$llama_lib" 2>/dev/null | grep -q "libcudart\.so\.12"; then
        echo ""
        return  # CPU-only build, no CUDA libs needed
    fi

    # Is libcudart.so.12 already resolvable? If so, nothing to do.
    if ldconfig -p 2>/dev/null | grep -q "libcudart\.so\.12"; then
        echo ""
        return
    fi

    # Look for Ollama's bundled CUDA-12 runtime.
    if [ -f /usr/local/lib/ollama/cuda_v12/libcudart.so.12 ]; then
        echo "/usr/local/lib/ollama/cuda_v12"
        return
    fi

    # Couldn't find a fallback - sidecar will fail to load the model.
    echo ""
}

start_python_sidecar() {
    step "Starting Python sidecar"

    if is_running "$PY_PID"; then
        ok "python sidecar already running (pid $(cat "$PY_PID"))"
        return
    fi

    # CUDA library path resolution (Linux GPU contributors).
    local cuda_lib_path
    cuda_lib_path="$(resolve_cuda_lib_path)"
    if [ -n "$cuda_lib_path" ]; then
        info "llama-cpp-python needs libcudart.so.12 - found at $cuda_lib_path (Ollama bundle), prepending to LD_LIBRARY_PATH"
        export LD_LIBRARY_PATH="$cuda_lib_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi

    info "starting on 0.0.0.0:$PYTHON_PORT in background (log: $PY_LOG)"
    cd "$PY_DIR"
    nohup "$VENV_PY" -m potluck_local.app --port "$PYTHON_PORT" \
        >"$PY_LOG" 2>&1 &
    local pid=$!
    echo "$pid" >"$PY_PID"
    cd - >/dev/null

    # Wait for /health to return 200.
    local i=0
    while [ $i -lt 20 ]; do
        if curl -fsS --max-time 1 "http://127.0.0.1:$PYTHON_PORT/health" >/dev/null 2>&1; then
            ok "python sidecar healthy (pid $pid, port $PYTHON_PORT)"
            return
        fi
        sleep 0.5
        i=$((i + 1))
    done
    warn "python sidecar didn't return /health in 10s - recent log:"
    tail -n 20 "$PY_LOG" | sed 's/^/      | /' >&2
    fail "python sidecar failed to start"
}

# ----------------------------------------------------------------------------
# Electron desktop app launch (GUI mode only)
# ----------------------------------------------------------------------------

launch_electron_app() {
    step "Launching Electron desktop app"
    if is_running "$APP_PID"; then
        ok "electron app already running (pid $(cat "$APP_PID"))"
        return
    fi
    info "starting electron in background (log: $APP_LOG)"
    cd "$ELECTRON_DIR"
    nohup pnpm exec electron . >"$APP_LOG" 2>&1 &
    local pid=$!
    echo "$pid" >"$APP_PID"
    cd - >/dev/null
    # No health probe - Electron's window-ready event isn't directly
    # observable from outside. Give it a beat then check the process
    # is still alive (i.e. didn't crash on startup).
    sleep 2
    if ! is_running "$APP_PID"; then
        warn "electron app exited shortly after launch. Last log lines:"
        tail -n 20 "$APP_LOG" | sed 's/^/      | /' >&2
        fail "electron app failed to start"
    fi
    ok "electron app launched (pid $pid)"
    info "use the app's Household view to pair this machine + manage settings"
}

# ----------------------------------------------------------------------------
# Interactive pairing + model load + contributor toggle
# ----------------------------------------------------------------------------

call_ipc() {
    # call_ipc METHOD PATH [JSON_BODY]  -> echoes response body
    local method="$1"
    local path="$2"
    local body="${3:-}"
    if [ -n "$body" ]; then
        curl --silent --unix-socket "$MESH_SOCKET" \
            -X "$method" "http://localhost$path" \
            -H 'Content-Type: application/json' \
            -d "$body"
    else
        curl --silent --unix-socket "$MESH_SOCKET" \
            -X "$method" "http://localhost$path"
    fi
}

call_python() {
    local method="$1"
    local path="$2"
    local body="${3:-}"
    if [ -n "$body" ]; then
        curl --silent -X "$method" "http://127.0.0.1:$PYTHON_PORT$path" \
            -H 'Content-Type: application/json' \
            -d "$body"
    else
        curl --silent -X "$method" "http://127.0.0.1:$PYTHON_PORT$path"
    fi
}

pair_with_household() {
    step "Pair this machine into your household"

    # Already enrolled? The mesh sidecar's /status will tell us.
    local status_json
    status_json="$(call_ipc GET /status || echo '{}')"
    if echo "$status_json" | grep -q '"mesh_status":"running"'; then
        local mesh_ip
        mesh_ip=$(echo "$status_json" | grep -oE '"mesh_ip":"[^"]*"' | cut -d'"' -f4)
        ok "already paired (mesh IP $mesh_ip)"
        return
    fi

    info "On your laptop, open the Potluck app → Household → Add another machine."
    info "Copy the token shown, then paste it here:"
    local token
    token="$(ask "Pairing token")"
    [ -n "$token" ] || fail "no token entered - re-run with --run-only"

    info "calling POST /pair"
    local resp
    resp="$(call_ipc POST /pair "{\"pairing_token\": \"$token\"}")"
    if echo "$resp" | grep -q '"mesh_ip"'; then
        local ip
        ip=$(echo "$resp" | grep -oE '"mesh_ip":"[^"]*"' | cut -d'"' -f4)
        ok "paired - this machine is $ip on the household mesh"
    else
        warn "pair returned: $resp"
        fail "pairing failed - check the token and try again"
    fi
}

load_model() {
    step "Pick a model to load"

    # If a model is already loaded, skip.
    local health
    health="$(call_python GET /health || echo '{}')"
    local current
    current=$(echo "$health" | grep -oE '"active_model_id":"[^"]*"' | cut -d'"' -f4 || echo "")
    if [ -n "$current" ] && [ "$current" != "null" ]; then
        ok "model already loaded: $current"
        return
    fi

    info "fetching available models from the local sidecar"
    local models_json
    models_json="$(call_python GET /models)"

    # Extract installed model ids
    local installed
    installed=$(echo "$models_json" | python3 -c '
import json, sys
data = json.load(sys.stdin)
ids = [m["id"] for m in data if m.get("installed")]
for i in ids:
    print(i)
' 2>/dev/null || echo "")

    if [ -z "$installed" ]; then
        warn "no models installed yet"
        local default_slug="qwen2.5:3b-instruct-q4_K_M"
        local install_default
        install_default="$(ask "Install $default_slug now? (~2GB download)" "y")"
        if [ "${install_default,,}" != "y" ] && [ "${install_default,,}" != "yes" ]; then
            info "skip - install one yourself via:"
            info "  curl -X POST http://127.0.0.1:$PYTHON_PORT/models/<slug>/install"
            info "then re-run: $0 --run-only"
            fail "no installed model to load"
        fi
        info "kicking off install (this can take several minutes)"
        call_python POST "/models/$default_slug/install" >/dev/null
        # Poll download progress until 100% or failure
        local last_pct=-1
        while true; do
            local prog
            prog="$(call_python GET "/models/$default_slug/download-progress" 2>/dev/null || echo '')"
            local pct
            pct=$(echo "$prog" | grep -oE '"progress_percent":[0-9.]+' | cut -d':' -f2)
            local state
            state=$(echo "$prog" | grep -oE '"state":"[^"]*"' | cut -d'"' -f4)
            if [ "$state" = "complete" ] || [ "$state" = "installed" ]; then
                ok "downloaded $default_slug"
                break
            fi
            if [ "$state" = "error" ] || [ "$state" = "failed" ]; then
                warn "install failed: $prog"
                fail "model install failed"
            fi
            if [ -n "$pct" ] && [ "$pct" != "$last_pct" ]; then
                # Truncate to int for cleaner output
                local pct_int=${pct%.*}
                printf "    download: %s%%\r" "$pct_int"
                last_pct="$pct"
            fi
            sleep 2
        done
        echo  # finish the \r line
        installed="$default_slug"
    fi

    echo
    echo "    Installed models:"
    local n=1
    while IFS= read -r m; do
        printf "      %d) %s\n" "$n" "$m"
        n=$((n + 1))
    done <<< "$installed"

    local choice
    choice="$(ask "Which one to load (number)" "1")"
    local picked
    picked=$(echo "$installed" | sed -n "${choice}p")
    [ -n "$picked" ] || fail "invalid choice"

    info "loading $picked (this can take a minute on first load)"
    local load_resp
    load_resp="$(call_python POST "/models/$picked/load")"
    if echo "$load_resp" | grep -q '"load_time_seconds"'; then
        ok "loaded $picked"
    else
        warn "load returned: $load_resp"
        fail "model load failed - see $PY_LOG"
    fi
}

enable_contributor_mode() {
    step "Enabling contributor mode"
    local resp
    resp="$(call_python PUT /settings/network '{"contributor_mode": true}')"
    if echo "$resp" | grep -q '"contributor_mode":true'; then
        ok "contributor_mode = true"
    else
        warn "PUT /settings/network returned: $resp"
        fail "could not enable contributor mode"
    fi
}

# ----------------------------------------------------------------------------
# Stop / status
# ----------------------------------------------------------------------------

stop_sidecars() {
    step "Stopping sidecars"
    if is_running "$APP_PID"; then
        local pid; pid=$(cat "$APP_PID")
        kill "$pid" 2>/dev/null || true
        ok "electron app stopped (was pid $pid)"
        rm -f "$APP_PID"
    else
        info "electron app wasn't running"
    fi
    if is_running "$PY_PID"; then
        local pid; pid=$(cat "$PY_PID")
        kill "$pid" 2>/dev/null || true
        ok "python sidecar stopped (was pid $pid)"
        rm -f "$PY_PID"
    else
        info "python sidecar wasn't running"
    fi
    if is_running "$MESH_PID"; then
        local pid; pid=$(cat "$MESH_PID")
        sudo kill "$pid" 2>/dev/null || true
        ok "mesh sidecar stopped (was pid $pid)"
        rm -f "$MESH_PID"
    else
        info "mesh sidecar wasn't running"
    fi
}

show_status() {
    step "Sidecar status"
    if is_running "$APP_PID"; then
        ok "electron app UP (pid $(cat "$APP_PID"))"
    else
        info "electron app DOWN"
    fi
    if is_running "$MESH_PID"; then
        ok "mesh sidecar UP (pid $(cat "$MESH_PID"))"
        local status; status="$(call_ipc GET /status 2>/dev/null || echo '')"
        if [ -n "$status" ]; then
            local ip; ip=$(echo "$status" | grep -oE '"mesh_ip":"[^"]*"' | cut -d'"' -f4)
            [ -n "$ip" ] && info "mesh IP: $ip"
        fi
    else
        info "mesh sidecar DOWN"
    fi
    if is_running "$PY_PID"; then
        ok "python sidecar UP (pid $(cat "$PY_PID"))"
        local h; h="$(call_python GET /health 2>/dev/null || echo '')"
        if [ -n "$h" ]; then
            local m; m=$(echo "$h" | grep -oE '"active_model_id":"[^"]*"' | cut -d'"' -f4)
            [ -n "$m" ] && [ "$m" != "null" ] && info "loaded model: $m"
        fi
        local caps; caps="$(call_python GET /peer/capabilities 2>/dev/null || echo '')"
        if [ -n "$caps" ]; then
            local avail; avail=$(echo "$caps" | grep -oE '"available_for_inference":(true|false)' | cut -d':' -f2)
            info "available_for_inference: $avail"
        fi
    else
        info "python sidecar DOWN"
    fi
}

# ----------------------------------------------------------------------------
# Final summary
# ----------------------------------------------------------------------------

print_summary() {
    local mesh_ip=""
    if [ -S "$MESH_SOCKET" ]; then
        mesh_ip=$(call_ipc GET /status 2>/dev/null | grep -oE '"mesh_ip":"[^"]*"' | cut -d'"' -f4)
    fi
    echo
    printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" "$C_CYAN" "$C_OFF"
    printf "%s Potluck household contributor is RUNNING %s\n" "$C_GREEN" "$C_OFF"
    printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" "$C_CYAN" "$C_OFF"
    if [ -n "$mesh_ip" ]; then
        echo "  Mesh IP   : $mesh_ip"
    fi
    echo "  Mesh PID  : $(cat "$MESH_PID" 2>/dev/null || echo '?')   log: $MESH_LOG"
    echo "  Python PID: $(cat "$PY_PID" 2>/dev/null || echo '?')   log: $PY_LOG"
    echo
    echo "  On your laptop, open the Potluck app → Chat. The Household toggle"
    echo "  in the top bar should now be enabled. Click it, send a prompt,"
    echo "  and it'll stream from this machine."
    echo
    echo "  To stop: $0 --stop"
    echo "  To check status: $0 --status"
    echo
}

# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

MODE="full"
# GUI default: 'on' if a display is detected, 'off' on headless boxes.
# Override with --gui or --headless. The check looks for $DISPLAY (X11) or
# $WAYLAND_DISPLAY (Wayland) - both unset on a TTY-only / SSH-into-server
# session, which is the natural "headless contributor" case.
if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    GUI="on"
else
    GUI="off"
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --setup-only) MODE="setup" ;;
        --run-only)   MODE="run" ;;
        --stop)       MODE="stop" ;;
        --status)     MODE="status" ;;
        --gui)        GUI="on" ;;
        --headless)   GUI="off" ;;
        -h|--help)
            sed -n '2,40p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) fail "unknown flag: $1" ;;
    esac
    shift
done

detect_distro

if [ "$MODE" = "stop" ]; then
    stop_sidecars
    exit 0
fi
if [ "$MODE" = "status" ]; then
    show_status
    exit 0
fi

step "Potluck household contributor - Linux ($DISTRO_FAMILY family, gui=$GUI)"
info "repo root: $REPO_ROOT"

# Pre-flight checks: catch environmental issues (VPN, Python version, network,
# coordinator reachability) before any installs run. These conditions
# otherwise fail silently mid-install.
# `--auto-fix` disconnects Mullvad if connected; leave it off if you'd rather
# disconnect manually.
PREFLIGHT="$REPO_ROOT/scripts/setup-household-preflight.sh"
if [ -x "$PREFLIGHT" ]; then
    step "Running pre-flight checks"
    if ! "$PREFLIGHT" --auto-fix; then
        fail "pre-flight checks failed (see above). Fix the listed issues then re-run."
    fi
else
    warn "preflight script not found at $PREFLIGHT; continuing without environment checks"
fi

if [ "$MODE" = "setup" ] || [ "$MODE" = "full" ]; then
    install_system_deps
    install_go
    pick_python
    build_mesh_sidecar
    setup_python_venv
    if [ "$GUI" = "on" ]; then
        install_node_pnpm
        build_electron_app
        create_desktop_entry
    fi
fi

if [ "$MODE" = "run" ] || [ "$MODE" = "full" ]; then
    [ -x "$MESH_BIN" ] || fail "mesh binary not built - run with --setup-only first"
    [ -x "$VENV_PY" ]  || fail "python venv missing - run with --setup-only first"
    # PYTHON_BIN is needed for pick_python errors but venv is what we actually use here
    PYTHON_BIN="$VENV_PY"

    start_mesh_sidecar
    start_python_sidecar
    if [ "$GUI" = "on" ]; then
        # GUI users pair via the in-app Household view, not the CLI prompts.
        launch_electron_app
    else
        # Headless users go through the CLI pairing flow.
        pair_with_household
        load_model
        enable_contributor_mode
    fi
    print_summary
fi

if [ "$MODE" = "setup" ]; then
    step "Setup complete"
    info "next: $0 --run-only"
fi
