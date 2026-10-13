#!/usr/bin/env bash
#
# setup-dev.sh
#
# One-shot dev environment setup for the Potluck repo. Run this once after
# `git clone`, then `make dev` (or `make desktop`) to bring up everything.
#
# What it does:
#   1. Verifies your toolchain (Python 3.11+, pnpm, docker).
#   2. Creates Python venvs for the desktop sidecar + cloud API and
#      installs requirements.
#   3. Installs the frontend + Electron node_modules.
#   4. Brings up Postgres in Docker and runs Alembic migrations.
#
# Idempotent - safe to re-run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ----------------------------------------------------------------------------
# Output helpers (matches setup-household.sh)
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

PYTHON_BIN="${PYTHON_BIN:-python3}"

# ----------------------------------------------------------------------------
# Step 1: toolchain check
# ----------------------------------------------------------------------------

step "Checking your toolchain"

# Python 3.11+
if ! command -v "$PYTHON_BIN" >/dev/null; then
    fail "$PYTHON_BIN not found. Install Python 3.11+ first.
        - Mac:   brew install python@3.11
        - Linux: see your distro's python3 package
        - Or override with: PYTHON_BIN=python3.11 $0"
fi
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null; then
    fail "$PYTHON_BIN is too old. Need 3.11+. Got: $("$PYTHON_BIN" --version 2>&1)"
fi
ok "$PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

# pnpm
if ! command -v pnpm >/dev/null; then
    fail "pnpm not found. Install with:
        - Mac:   brew install pnpm
        - Linux: npm install -g pnpm  (after installing nodejs)
        - Or:    curl -fsSL https://get.pnpm.io/install.sh | sh -"
fi
ok "pnpm $(pnpm --version)"

# Docker (for Postgres)
if ! command -v docker >/dev/null; then
    fail "docker not found. Install Docker Desktop (Mac/Windows) or your distro's docker package."
fi
if ! docker info >/dev/null 2>&1; then
    fail "docker is installed but the daemon isn't responding.
        - Mac/Windows: open Docker Desktop and wait for it to start.
        - Linux:       'sudo systemctl start docker'"
fi
ok "docker daemon responding"

# ----------------------------------------------------------------------------
# Step 2: Python venvs
# ----------------------------------------------------------------------------

setup_python_venv() {
    local dir="$1"
    local label="$2"
    if [ ! -d "$dir/.venv" ]; then
        info "creating venv at $dir/.venv"
        "$PYTHON_BIN" -m venv "$dir/.venv"
    else
        info "venv exists at $dir/.venv"
    fi
    # shellcheck disable=SC1091
    info "installing/upgrading dependencies (this can take a minute)"
    (
        # shellcheck disable=SC1091
        source "$dir/.venv/bin/activate"
        pip install --quiet --upgrade pip
        pip install --quiet -r "$dir/requirements.txt"
        deactivate
    )
    ok "$label venv ready"
}

step "Python sidecar venv (apps/desktop/python)"
setup_python_venv "apps/desktop/python" "desktop sidecar"

step "Cloud API venv (services/api)"
setup_python_venv "services/api" "cloud API"

# ----------------------------------------------------------------------------
# Step 3: Node deps
# ----------------------------------------------------------------------------

step "Frontend dependencies (apps/desktop/frontend)"
(cd apps/desktop/frontend && pnpm install --silent)
ok "frontend node_modules ready"

step "Electron dependencies (apps/desktop/electron)"
(cd apps/desktop/electron && pnpm install --silent)
ok "electron node_modules ready"

# ----------------------------------------------------------------------------
# Step 4: Postgres + migrations
# ----------------------------------------------------------------------------

step "Postgres (via docker compose)"
docker compose up -d postgres >/dev/null
info "waiting for Postgres to be healthy…"
for _ in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U potluck -d potluck >/dev/null 2>&1; then
        ok "Postgres is healthy"
        break
    fi
    sleep 1
done

step "Running Alembic migrations"
(
    cd services/api
    # shellcheck disable=SC1091
    source .venv/bin/activate
    alembic upgrade head >/dev/null
    deactivate
)
ok "migrations applied"

# ----------------------------------------------------------------------------
# Step 5: summary
# ----------------------------------------------------------------------------

cat <<EOF

${C_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_OFF}
${C_GREEN} Dev environment ready ${C_OFF}
${C_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_OFF}

  Single command to bring everything up:
      ${C_GREEN}make dev${C_OFF}    ${C_DIM}# backend + desktop app stack${C_OFF}

  Desktop app only:
      ${C_GREEN}make desktop${C_OFF}

  Manual / individual services:
      Cloud API:   ${C_DIM}cd services/api && source .venv/bin/activate && \\
                   uvicorn potluck_api.app:create_app --factory --reload --port 8080${C_OFF}
      Desktop:     ${C_DIM}cd apps/desktop/electron && pnpm dev${C_OFF}
                   ${C_DIM}(starts Vite + Python sidecar via concurrently)${C_OFF}

EOF
