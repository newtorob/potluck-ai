#!/usr/bin/env bash
#
# install-persistence.sh
#
# Install (or remove) systemd units that keep the Potluck household mesh
# running across reboots, plus the firewall accept rules that let it
# coexist with Tailscale and Mullvad.
#
# WHEN TO RUN THIS
# -----------------
# Run AFTER setup-household.sh has built the mesh binary and paired this
# machine into a household. This script doesn't pair, doesn't build —
# it just wires up the already-working system to survive a reboot.
#
# USAGE
# ------
#   sudo ./install-persistence.sh --install --coordinator-url http://laptop:8400
#   sudo ./install-persistence.sh --uninstall
#   sudo ./install-persistence.sh --status
#
# WHAT IT DOES (install)
# -----------------------
#   1. Copies install-firewall-rules.sh to /usr/local/lib/potluck/
#   2. Renders potluck-firewall.service from template, installs to
#      /etc/systemd/system/, enables + starts.
#   3. Renders potluck-mesh.service from template (filling in mesh binary
#      path, coordinator URL, derived DERP URL, user's UID/GID), installs,
#      enables + starts.
#   4. Verifies both units are active.
#
# WHAT IT DOES (uninstall)
# -------------------------
#   1. Stops + disables both units, removes the unit files.
#   2. Runs install-firewall-rules.sh --uninstall to delete nft rules.
#   3. Removes /usr/local/lib/potluck/install-firewall-rules.sh.
#   4. Does NOT touch your $HOME/.potluck state, pairing tokens, etc.
#
# Re-running --install is safe — service files are overwritten with current
# values, which is what you want if the mesh binary moved or the coordinator
# URL changed.

set -euo pipefail

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

MESH_BIN_DEFAULT="$REPO_ROOT/apps/desktop/go/potluck-mesh/dist/potluck-mesh"

# System install locations
LIB_DIR="/usr/local/lib/potluck"
FIREWALL_SCRIPT_DST="$LIB_DIR/install-firewall-rules.sh"
SYSTEMD_DIR="/etc/systemd/system"
FIREWALL_UNIT_DST="$SYSTEMD_DIR/potluck-firewall.service"
MESH_UNIT_DST="$SYSTEMD_DIR/potluck-mesh.service"

# Templates (in this repo)
FIREWALL_SCRIPT_SRC="$SCRIPT_DIR/install-firewall-rules.sh"
FIREWALL_TEMPLATE="$SCRIPT_DIR/potluck-firewall.service.template"
MESH_TEMPLATE="$SCRIPT_DIR/potluck-mesh.service.template"

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

if [ -t 1 ]; then
    C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_RED=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
    C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_OFF=""
fi

step() { printf "\n%s==> %s%s\n" "$C_CYAN" "$1" "$C_OFF"; }
ok()   { printf "    %s[ok]%s %s\n"   "$C_GREEN"  "$C_OFF" "$1"; }
info() { printf "    %s%s%s\n"        "$C_DIM"    "$1" "$C_OFF"; }
warn() { printf "    %s[warn]%s %s\n" "$C_YELLOW" "$C_OFF" "$1"; }
fail() { printf "    %s[fail]%s %s\n" "$C_RED"    "$C_OFF" "$1" >&2; exit 1; }

require_root() {
    if [ "$EUID" -ne 0 ]; then
        fail "must run as root (writes to /etc/systemd/system and /usr/local/lib). Try: sudo $0 $*"
    fi
}

require_systemd() {
    command -v systemctl >/dev/null || fail "systemctl not found; this script is Linux-systemd-only"
}

# Derive DERP URL from coordinator URL the same way setup-household.sh does.
# coord=http://192.168.1.10:8400 → derp=tcp+insecure://192.168.1.10:8443
derive_derp_url() {
    local coord="$1"
    local host
    host=$(echo "$coord" | sed -E 's|https?://([^/:]+).*|\1|')
    echo "tcp+insecure://${host}:8443"
}

# ----------------------------------------------------------------------------
# Install
# ----------------------------------------------------------------------------

do_install() {
    local coord_url="$1"
    local mesh_bin="$2"
    local derp_url="$3"
    local sudo_uid="$4"
    local sudo_gid="$5"

    [ -x "$mesh_bin" ] || fail "mesh binary not executable at $mesh_bin — run setup-household.sh first"
    [ -f "$FIREWALL_SCRIPT_SRC" ] || fail "firewall script template missing: $FIREWALL_SCRIPT_SRC"
    [ -f "$FIREWALL_TEMPLATE" ] || fail "firewall unit template missing: $FIREWALL_TEMPLATE"
    [ -f "$MESH_TEMPLATE" ] || fail "mesh unit template missing: $MESH_TEMPLATE"

    step "Installing firewall helper script to $LIB_DIR"
    mkdir -p "$LIB_DIR"
    install -m 0755 "$FIREWALL_SCRIPT_SRC" "$FIREWALL_SCRIPT_DST"
    ok "installed $FIREWALL_SCRIPT_DST"

    step "Rendering systemd unit files"
    # Use a unique sed delimiter so URLs containing / don't break us.
    sed -e "s|@FIREWALL_SCRIPT@|$FIREWALL_SCRIPT_DST|g" \
        "$FIREWALL_TEMPLATE" > "$FIREWALL_UNIT_DST"
    chmod 0644 "$FIREWALL_UNIT_DST"
    ok "wrote $FIREWALL_UNIT_DST"

    sed -e "s|@MESH_BIN@|$mesh_bin|g" \
        -e "s|@COORD_URL@|$coord_url|g" \
        -e "s|@DERP_URL@|$derp_url|g" \
        -e "s|@SUDO_UID@|$sudo_uid|g" \
        -e "s|@SUDO_GID@|$sudo_gid|g" \
        "$MESH_TEMPLATE" > "$MESH_UNIT_DST"
    chmod 0644 "$MESH_UNIT_DST"
    ok "wrote $MESH_UNIT_DST"

    step "Reloading systemd + enabling units"
    systemctl daemon-reload
    systemctl enable potluck-firewall.service potluck-mesh.service >/dev/null
    ok "enabled potluck-firewall.service + potluck-mesh.service"

    step "Starting services"
    # Firewall first (mesh Requires=potluck-firewall.service so it'd start
    # it anyway, but doing it explicitly gives clearer error messages).
    if systemctl restart potluck-firewall.service; then
        ok "potluck-firewall.service active"
    else
        warn "firewall service failed to start; mesh service may follow:"
        journalctl -u potluck-firewall.service -n 20 --no-pager | sed 's/^/      | /' >&2
    fi
    if systemctl restart potluck-mesh.service; then
        ok "potluck-mesh.service active"
    else
        warn "mesh service failed to start; recent log:"
        journalctl -u potluck-mesh.service -n 20 --no-pager | sed 's/^/      | /' >&2
        fail "mesh unit failed; fix the issue then re-run --install"
    fi

    step "Verification"
    "$FIREWALL_SCRIPT_DST" --status

    echo
    printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" "$C_CYAN" "$C_OFF"
    printf "%s Potluck mesh persistence is INSTALLED %s\n" "$C_GREEN" "$C_OFF"
    printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" "$C_CYAN" "$C_OFF"
    info "Mesh will start at boot and the firewall accept rules will be"
    info "reinstalled. To uninstall: sudo $0 --uninstall"
    info ""
    info "Check status:    systemctl status potluck-mesh.service"
    info "Tail logs:       journalctl -u potluck-mesh.service -f"
}

# ----------------------------------------------------------------------------
# Uninstall
# ----------------------------------------------------------------------------

do_uninstall() {
    step "Stopping + disabling units"
    if systemctl is-enabled potluck-mesh.service >/dev/null 2>&1; then
        systemctl disable --now potluck-mesh.service >/dev/null 2>&1 || true
        ok "potluck-mesh.service disabled + stopped"
    else
        info "potluck-mesh.service was not enabled"
    fi
    if systemctl is-enabled potluck-firewall.service >/dev/null 2>&1; then
        systemctl disable --now potluck-firewall.service >/dev/null 2>&1 || true
        ok "potluck-firewall.service disabled + stopped"
    else
        info "potluck-firewall.service was not enabled"
    fi

    step "Removing unit files"
    for f in "$MESH_UNIT_DST" "$FIREWALL_UNIT_DST"; do
        if [ -f "$f" ]; then
            rm -f "$f"
            ok "removed $f"
        fi
    done
    systemctl daemon-reload

    step "Removing firewall accept rules from running ruleset"
    if [ -x "$FIREWALL_SCRIPT_DST" ]; then
        "$FIREWALL_SCRIPT_DST" --uninstall || warn "firewall --uninstall reported non-zero; check by hand"
    elif [ -x "$FIREWALL_SCRIPT_SRC" ]; then
        info "system copy already gone; using in-repo copy"
        "$FIREWALL_SCRIPT_SRC" --uninstall || warn "firewall --uninstall reported non-zero; check by hand"
    else
        warn "no firewall script found to clean up rules; remove by hand if needed"
    fi

    step "Removing firewall helper script"
    if [ -f "$FIREWALL_SCRIPT_DST" ]; then
        rm -f "$FIREWALL_SCRIPT_DST"
        ok "removed $FIREWALL_SCRIPT_DST"
    fi
    # Don't rmdir $LIB_DIR — other Potluck installers may share it.

    echo
    ok "Potluck mesh persistence uninstalled"
    info "Your $HOME/.potluck state (pairings, logs) was NOT touched."
}

# ----------------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------------

do_status() {
    step "Persistence status"
    for u in potluck-firewall.service potluck-mesh.service; do
        local enabled active
        enabled=$(systemctl is-enabled "$u" 2>/dev/null || echo "not-installed")
        active=$(systemctl is-active "$u" 2>/dev/null || echo "inactive")
        printf "    %-32s enabled=%s active=%s\n" "$u" "$enabled" "$active"
    done
    if [ -x "$FIREWALL_SCRIPT_DST" ]; then
        echo
        "$FIREWALL_SCRIPT_DST" --status
    fi
}

# ----------------------------------------------------------------------------
# Entry
# ----------------------------------------------------------------------------

MODE=""
COORD_URL="${POTLUCK_COORDINATOR_URL:-}"
MESH_BIN="$MESH_BIN_DEFAULT"
DERP_URL="${POTLUCK_DERP_URL:-}"

# When invoked via sudo, SUDO_UID/SUDO_GID are set by sudo itself. When invoked
# as root directly (not via sudo), fall back to 0/0 — which means the IPC
# socket will be root-owned and CLI tools running as the user will need sudo.
# We warn in that case below.
TARGET_UID="${SUDO_UID:-}"
TARGET_GID="${SUDO_GID:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --install)          MODE="install" ;;
        --uninstall|--remove) MODE="uninstall" ;;
        --status)           MODE="status" ;;
        --coordinator-url)  shift; COORD_URL="${1:-}" ;;
        --derp-url)         shift; DERP_URL="${1:-}" ;;
        --mesh-bin)         shift; MESH_BIN="${1:-}" ;;
        --user-uid)         shift; TARGET_UID="${1:-}" ;;
        --user-gid)         shift; TARGET_GID="${1:-}" ;;
        -h|--help)
            sed -n '2,40p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) fail "unknown flag: $1" ;;
    esac
    shift
done

require_systemd

case "$MODE" in
    status)
        do_status
        exit 0
        ;;
    uninstall)
        require_root "$@"
        do_uninstall
        exit 0
        ;;
    install)
        require_root "$@"
        [ -n "$COORD_URL" ] || fail "--coordinator-url is required for --install (or set POTLUCK_COORDINATOR_URL)"
        [ -n "$DERP_URL" ] || DERP_URL=$(derive_derp_url "$COORD_URL")
        if [ -z "$TARGET_UID" ] || [ -z "$TARGET_GID" ]; then
            warn "SUDO_UID/SUDO_GID not set — IPC socket will be root-owned."
            warn "CLI tools running as a non-root user will need sudo to talk to it."
            warn "To fix: re-run via sudo (not as root directly), or pass --user-uid/--user-gid."
            TARGET_UID="${TARGET_UID:-0}"
            TARGET_GID="${TARGET_GID:-0}"
        fi
        info "coordinator URL : $COORD_URL"
        info "DERP URL        : $DERP_URL"
        info "mesh binary     : $MESH_BIN"
        info "socket owner    : uid=$TARGET_UID gid=$TARGET_GID"
        do_install "$COORD_URL" "$MESH_BIN" "$DERP_URL" "$TARGET_UID" "$TARGET_GID"
        ;;
    *)
        fail "no mode given. Use --install, --uninstall, or --status"
        ;;
esac
