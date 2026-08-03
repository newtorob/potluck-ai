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
#      path, coordinator URL, DERP relay URL, user's UID/GID), installs,
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
# Mesh binary is staged into a system path so the dedicated 'potluck' user
# can exec it. Running as User=potluck means the daemon can NOT traverse
# /home/sam (mode 0750) to read the binary out of the repo checkout.
MESH_BIN_DST="$LIB_DIR/potluck-mesh"
SYSTEMD_DIR="/etc/systemd/system"
FIREWALL_UNIT_DST="$SYSTEMD_DIR/potluck-firewall.service"
MESH_UNIT_DST="$SYSTEMD_DIR/potluck-mesh.service"

# Dedicated system user the mesh service runs as. Created by
# create_potluck_user() during --install. Owns /var/lib/potluck/mesh and
# the WG private key inside it.
POTLUCK_USER="potluck"
POTLUCK_STATE_DIR="/var/lib/potluck/mesh"
POTLUCK_KEY_PATH="$POTLUCK_STATE_DIR/wg.key"

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

# Production DERP relay fallback (services/derp, fly.toml app=potluck-derp,
# custom domain with its own cert — Fly terminates TLS on 443 and forwards
# cleartext to the relay binary on 8443, so the client-facing scheme is
# tcp+tls per internal/derp/client.go). This is the shipped default; it is
# NOT derived from --coordinator-url (a self-hosted/LAN coordinator does not
# imply a self-hosted relay). Override with --derp-url / POTLUCK_DERP_URL,
# e.g. for LAN-only dev against a local services/derp run without TLS.
DERP_URL_DEFAULT="tcp+tls://derp.trypotluck.ai:443"

# Create the dedicated 'potluck' system user if it doesn't exist.
# Idempotent — re-running --install is a no-op when the user is already there.
# We use --system (UID < 1000), --no-create-home, and a nologin shell so the
# account can't be used for interactive access.
create_potluck_user() {
    if id "$POTLUCK_USER" >/dev/null 2>&1; then
        info "system user '$POTLUCK_USER' already exists (uid=$(id -u "$POTLUCK_USER"))"
        return 0
    fi
    step "Creating system user '$POTLUCK_USER'"
    # /usr/sbin/nologin is standard on Debian/Ubuntu/RHEL. Fall back to
    # /sbin/nologin if it's there instead (some minimal images).
    local nologin="/usr/sbin/nologin"
    [ -x "$nologin" ] || nologin="/sbin/nologin"
    [ -x "$nologin" ] || nologin="/bin/false"
    useradd --system --no-create-home --shell "$nologin" "$POTLUCK_USER" \
        || fail "useradd failed for '$POTLUCK_USER'"
    ok "created system user '$POTLUCK_USER' (uid=$(id -u "$POTLUCK_USER"))"
}

# Migrate an existing WG private key from the old per-user location
# ($SUDO_USER's $HOME/.local/state/potluck/mesh/wg.key) into the new
# system-owned location ($POTLUCK_KEY_PATH).
#
# Behavior:
#   - Clean install (no old key)  → no-op
#   - Old key present, new absent → copy, chown potluck:potluck, chmod 0600
#   - Both present                → keep the new one untouched, warn loudly
#   - Old key absent              → no-op
#
# We do NOT delete the old key — operator deletes it after verifying the
# service comes up with the same public key. Silent identity changes are
# the footgun we're guarding against.
migrate_existing_key() {
    # Discover the canonical legacy key. In priority order:
    #   1. The home of the User= currently set in /etc/systemd/system/potluck-mesh.service
    #      — this is THE active key, because that's the user under whose $HOME
    #      the Go binary's default key path resolved.
    #   2. $SUDO_USER's $HOME — first-time install from setup-household.sh
    #      with no prior systemd unit.
    #   3. /root — belt-and-suspenders for ad-hoc `sudo -E` runs.
    #
    # Priority matters: multiple legacy keys can coexist (e.g. user ran
    # setup-household.sh interactively, THEN systemd installed under root —
    # both /home/sam/...wg.key and /root/...wg.key exist, but only /root's is
    # the active identity). Picking the wrong one silently re-enrolls the
    # node under a new public key. (Discovered 2026-05-26 on media server.)
    local old_key=""

    local existing_unit="/etc/systemd/system/potluck-mesh.service"
    if [ -f "$existing_unit" ]; then
        local prev_user
        prev_user=$(grep -E '^User=' "$existing_unit" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')
        # No User= line ⇒ systemd default of root.
        [ -n "$prev_user" ] || prev_user="root"
        # If the previous unit already ran as $POTLUCK_USER, the migration
        # is already done — no legacy lookup needed.
        if [ "$prev_user" != "$POTLUCK_USER" ]; then
            local prev_home
            if [ "$prev_user" = "root" ]; then
                prev_home="/root"
            else
                prev_home=$(getent passwd "$prev_user" 2>/dev/null | cut -d: -f6)
            fi
            if [ -n "$prev_home" ] && [ -d "$prev_home" ]; then
                local candidate="$prev_home/.local/state/potluck/mesh/wg.key"
                if [ -f "$candidate" ]; then
                    old_key="$candidate"
                    info "found active legacy key (previous unit ran as '$prev_user')"
                fi
            fi
        fi
    fi

    if [ -z "$old_key" ]; then
        local src_user="${SUDO_USER:-}"
        if [ -n "$src_user" ] && [ "$src_user" != "root" ]; then
            local src_home
            src_home=$(getent passwd "$src_user" | cut -d: -f6)
            if [ -n "$src_home" ] && [ -d "$src_home" ]; then
                local candidate="$src_home/.local/state/potluck/mesh/wg.key"
                if [ -f "$candidate" ]; then
                    old_key="$candidate"
                fi
            fi
        fi
    fi

    if [ -z "$old_key" ] && [ -f "/root/.local/state/potluck/mesh/wg.key" ]; then
        old_key="/root/.local/state/potluck/mesh/wg.key"
        info "found legacy key under /root (User=root install)"
    fi

    if [ -z "$old_key" ]; then
        info "no pre-existing WG key found; clean install"
        return 0
    fi

    step "Migrating WireGuard private key to system location"
    info "found existing key: $old_key"

    if [ -f "$POTLUCK_KEY_PATH" ]; then
        warn "key already exists at $POTLUCK_KEY_PATH — NOT overwriting."
        warn "If you want the old key to win, run:"
        warn "  sudo cp '$old_key' '$POTLUCK_KEY_PATH'"
        warn "  sudo chown $POTLUCK_USER:$POTLUCK_USER '$POTLUCK_KEY_PATH'"
        warn "  sudo chmod 0600 '$POTLUCK_KEY_PATH'"
        warn "Leaving things alone — service will use the existing system key."
        return 0
    fi

    # StateDirectory= creates the dir at unit-start; do it ourselves now so
    # we have somewhere to copy into before the first service start.
    mkdir -p "$POTLUCK_STATE_DIR"
    cp "$old_key" "$POTLUCK_KEY_PATH"
    chown "$POTLUCK_USER:$POTLUCK_USER" "$POTLUCK_KEY_PATH"
    chmod 0600 "$POTLUCK_KEY_PATH"
    chown "$POTLUCK_USER:$POTLUCK_USER" "$POTLUCK_STATE_DIR"
    chmod 0700 "$POTLUCK_STATE_DIR"

    ok "copied key to $POTLUCK_KEY_PATH (owner=$POTLUCK_USER, mode=0600)"
    printf "\n%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" "$C_YELLOW" "$C_OFF"
    printf "%s KEY MIGRATED — preserving household identity %s\n" "$C_YELLOW" "$C_OFF"
    printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" "$C_YELLOW" "$C_OFF"
    info "Old key at $old_key was NOT deleted."
    info "Verify the service starts with the same public key, then remove it:"
    info "  rm '$old_key'"
    info ""
}

# Remove the 'potluck' system user. Only called from --uninstall --purge-user.
# Default uninstall leaves the user in place because it owns state files
# (the WG key under /var/lib/potluck/mesh).
remove_potluck_user() {
    if ! id "$POTLUCK_USER" >/dev/null 2>&1; then
        info "system user '$POTLUCK_USER' not present; nothing to remove"
        return 0
    fi
    step "Removing system user '$POTLUCK_USER' (--purge-user)"
    warn "this will leave $POTLUCK_STATE_DIR orphaned (owned by deleted UID)"
    warn "if you re-install later, the new 'potluck' user will get a fresh UID"
    warn "and may not be able to read the old WG key."
    userdel "$POTLUCK_USER" || fail "userdel failed for '$POTLUCK_USER'"
    ok "removed system user '$POTLUCK_USER'"
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

    # User + state must exist BEFORE we render the unit (which references
    # User=potluck) and BEFORE we migrate the key (which chowns to potluck).
    create_potluck_user
    migrate_existing_key

    step "Installing firewall helper script to $LIB_DIR"
    mkdir -p "$LIB_DIR"
    install -m 0755 "$FIREWALL_SCRIPT_SRC" "$FIREWALL_SCRIPT_DST"
    ok "installed $FIREWALL_SCRIPT_DST"

    step "Staging mesh binary to $MESH_BIN_DST"
    # Copy (don't symlink) so the running daemon under User=potluck doesn't
    # depend on /home/sam being traversable. Re-running --install picks up
    # a newer binary from the repo checkout automatically.
    install -m 0755 "$mesh_bin" "$MESH_BIN_DST"
    ok "staged $MESH_BIN_DST"

    step "Rendering systemd unit files"
    # Use a unique sed delimiter so URLs containing / don't break us.
    sed -e "s|@FIREWALL_SCRIPT@|$FIREWALL_SCRIPT_DST|g" \
        "$FIREWALL_TEMPLATE" > "$FIREWALL_UNIT_DST"
    chmod 0644 "$FIREWALL_UNIT_DST"
    ok "wrote $FIREWALL_UNIT_DST"

    sed -e "s|@MESH_BIN@|$MESH_BIN_DST|g" \
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
    local purge_user="${1:-no}"

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

    step "Removing staged binaries + scripts"
    if [ -f "$FIREWALL_SCRIPT_DST" ]; then
        rm -f "$FIREWALL_SCRIPT_DST"
        ok "removed $FIREWALL_SCRIPT_DST"
    fi
    if [ -f "$MESH_BIN_DST" ]; then
        rm -f "$MESH_BIN_DST"
        ok "removed $MESH_BIN_DST"
    fi
    # Don't rmdir $LIB_DIR — other Potluck installers may share it.

    if [ "$purge_user" = "yes" ]; then
        remove_potluck_user
    else
        info "Leaving system user '$POTLUCK_USER' and $POTLUCK_STATE_DIR in place."
        info "These hold the WG private key (household identity). Pass --purge-user"
        info "to also remove the user (you'll lose the household key)."
    fi

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
PURGE_USER="no"

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
        --purge-user)       PURGE_USER="yes" ;;
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
        do_uninstall "$PURGE_USER"
        exit 0
        ;;
    install)
        require_root "$@"
        [ -n "$COORD_URL" ] || fail "--coordinator-url is required for --install (or set POTLUCK_COORDINATOR_URL)"
        [ -n "$DERP_URL" ] || DERP_URL="$DERP_URL_DEFAULT"
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
