#!/usr/bin/env bash
#
# install-sidecar.sh
#
# Install (or remove) the systemd unit that auto-starts the Potluck main
# sidecar — the FastAPI process at apps/desktop/python that serves memory,
# tasks, agent observability, and the local API on port 8321.
#
# This is the headless counterpart to the Electron app, which spawns the
# same Python process via main.js. On a headless household node (no Electron
# UI) you want this service so the sidecar comes up at boot.
#
# USAGE
# ------
#   sudo ./install-sidecar.sh --install                 # uses ./.venv + port 8321
#   sudo ./install-sidecar.sh --install --port 8321 --user rob
#   sudo ./install-sidecar.sh --uninstall
#   sudo ./install-sidecar.sh --status
#
# WHAT IT DOES (install)
#   1. Renders potluck-sidecar.service.template, filling in the invoking
#      user (or --user override), the repo path, and the bind port.
#   2. Installs to /etc/systemd/system/, daemon-reloads, enables + starts.
#   3. Polls /health on the configured port to confirm the service is live.
#
# WHAT IT DOES (uninstall)
#   1. Stops + disables the unit, removes the unit file, daemon-reloads.
#   2. Does NOT touch ~/.potluck data — that's user state.
#
# Re-running --install is safe — the unit file is overwritten with current
# values, the service is restarted to pick up changes.

set -euo pipefail

# ----------------------------------------------------------------------------
# Paths and defaults
# ----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SIDECAR_TEMPLATE="$SCRIPT_DIR/potluck-sidecar.service.template"
SYSTEMD_DIR="/etc/systemd/system"
SIDECAR_UNIT_DST="$SYSTEMD_DIR/potluck-sidecar.service"

# Defaults — overridable via flags
PORT_DEFAULT="8321"
SIDECAR_USER_DEFAULT="${SUDO_USER:-$USER}"

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

if [ -t 1 ]; then
    C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_RED=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
    C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_OFF=""
fi

log()  { printf "%s\n" "${C_CYAN}==>${C_OFF} $*"; }
ok()   { printf "%s\n" "${C_GREEN}✓${C_OFF} $*"; }
warn() { printf "%s\n" "${C_YELLOW}!${C_OFF} $*" >&2; }
err()  { printf "%s\n" "${C_RED}✗${C_OFF} $*" >&2; }

require_root() {
    if [ "$EUID" -ne 0 ]; then
        err "must run as root (sudo $0 $*)"
        exit 1
    fi
}

# ----------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------

ACTION=""
PORT="$PORT_DEFAULT"
SIDECAR_USER="$SIDECAR_USER_DEFAULT"

while [ $# -gt 0 ]; do
    case "$1" in
        --install)   ACTION="install" ;;
        --uninstall) ACTION="uninstall" ;;
        --status)    ACTION="status" ;;
        --port)      PORT="$2"; shift ;;
        --user)      SIDECAR_USER="$2"; shift ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) err "unknown flag: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$ACTION" ]; then
    err "must specify --install, --uninstall, or --status"
    exit 1
fi

# ----------------------------------------------------------------------------
# Render + install
# ----------------------------------------------------------------------------

resolve_group_of_user() {
    local user="$1"
    id -gn "$user" 2>/dev/null || echo "$user"
}

render_unit() {
    local user="$1"
    local group="$2"
    local port="$3"

    if [ ! -f "$SIDECAR_TEMPLATE" ]; then
        err "template not found: $SIDECAR_TEMPLATE"
        exit 1
    fi

    sed \
        -e "s|@SIDECAR_USER@|$user|g" \
        -e "s|@SIDECAR_GROUP@|$group|g" \
        -e "s|@REPO_PATH@|$REPO_ROOT|g" \
        -e "s|@PORT@|$port|g" \
        "$SIDECAR_TEMPLATE"
}

do_install() {
    require_root

    if ! id "$SIDECAR_USER" >/dev/null 2>&1; then
        err "user '$SIDECAR_USER' does not exist on this system"
        exit 1
    fi

    local group
    group="$(resolve_group_of_user "$SIDECAR_USER")"

    local venv_python="$REPO_ROOT/apps/desktop/python/.venv/bin/python"
    if [ ! -x "$venv_python" ]; then
        err "venv python not found at $venv_python"
        err "run 'make sidecar-setup' first to create the venv"
        exit 1
    fi

    log "Rendering potluck-sidecar.service"
    log "  user=$SIDECAR_USER  group=$group  repo=$REPO_ROOT  port=$PORT"

    render_unit "$SIDECAR_USER" "$group" "$PORT" > "$SIDECAR_UNIT_DST"
    chmod 0644 "$SIDECAR_UNIT_DST"
    ok "wrote $SIDECAR_UNIT_DST"

    log "Reloading systemd"
    systemctl daemon-reload

    log "Enabling + starting potluck-sidecar.service"
    systemctl enable potluck-sidecar.service >/dev/null
    systemctl restart potluck-sidecar.service

    log "Waiting for /health on port $PORT (up to 30s)"
    local i=0
    while [ $i -lt 30 ]; do
        if curl -sS --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
            ok "sidecar is live on port $PORT"
            curl -sS "http://127.0.0.1:$PORT/health"
            echo
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done

    err "sidecar did not respond on port $PORT within 30s"
    err "check: journalctl -u potluck-sidecar --no-pager -n 50"
    exit 1
}

do_uninstall() {
    require_root

    if systemctl list-unit-files potluck-sidecar.service >/dev/null 2>&1; then
        log "Stopping + disabling potluck-sidecar.service"
        systemctl disable --now potluck-sidecar.service 2>/dev/null || true
    fi

    if [ -f "$SIDECAR_UNIT_DST" ]; then
        log "Removing $SIDECAR_UNIT_DST"
        rm -f "$SIDECAR_UNIT_DST"
        systemctl daemon-reload
        ok "uninstalled"
    else
        warn "no unit file at $SIDECAR_UNIT_DST — nothing to remove"
    fi
}

do_status() {
    if ! systemctl list-unit-files potluck-sidecar.service >/dev/null 2>&1; then
        warn "potluck-sidecar.service is not installed"
        exit 1
    fi
    systemctl status potluck-sidecar.service --no-pager || true
    echo
    log "Recent logs (last 20):"
    journalctl -u potluck-sidecar.service --no-pager -n 20 || true
}

case "$ACTION" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
    status)    do_status ;;
esac
