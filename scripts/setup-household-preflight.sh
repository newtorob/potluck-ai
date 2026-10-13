#!/usr/bin/env bash
#
# setup-household-preflight.sh
#
# Pre-flight environment check for Potluck household contributor setup on Linux.
# Sister script to setup-household-preflight.ps1 (Windows).
#
# Runs in under a minute. Catches the environmental issues that historically
# cause setup-household.sh to fail mid-install:
#
#   1. Active VPN (Mullvad, OpenVPN, generic WireGuard non-mesh) that
#      blackholes apt/pip/npm traffic.
#   2. Python 3.11+ availability. Ubuntu 22.04 ships 3.10 and we need 3.11.
#   3. sudo availability for the mesh-sidecar TUN creation.
#   4. Network endpoint reachability (PyPI, GitHub, npm registry).
#   5. Coordinator at the laptop's LAN IP (if provided).
#   6. Disk space.
#   7. Display detection for GUI vs headless mode hinting.
#
# Each check prints FAIL / WARN / OK with a one-line "how to fix" message.
# Exit code: 0 if no FAIL conditions, 1 otherwise.
#
# Usage:
#   ./scripts/setup-household-preflight.sh
#   ./scripts/setup-household-preflight.sh --coordinator-url http://192.168.1.10:8400
#   ./scripts/setup-household-preflight.sh --auto-fix    # disconnect Mullvad if connected

set -uo pipefail

# ----------------------------------------------------------------------------
# Parse args
# ----------------------------------------------------------------------------

COORDINATOR_URL="${POTLUCK_COORDINATOR_URL:-}"
AUTO_FIX=0
while [ $# -gt 0 ]; do
    case "$1" in
        --coordinator-url) COORDINATOR_URL="$2"; shift 2 ;;
        --auto-fix)        AUTO_FIX=1; shift ;;
        -h|--help)         sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown flag: $1" >&2; exit 1 ;;
    esac
done

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

header() { printf "\n%s==> %s%s\n" "$C_CYAN" "$1" "$C_OFF"; }
ok()     { printf "    %s[ok]%s   %s\n"   "$C_GREEN"  "$C_OFF" "$1"; }
info()   { printf "    %s%s%s\n"          "$C_DIM"    "$1" "$C_OFF"; }
warn()   { printf "    %s[warn]%s %s\n"   "$C_YELLOW" "$C_OFF" "$1"; }
fail_check() { printf "    %s[fail]%s %s\n" "$C_RED"  "$C_OFF" "$1"; }

FAILURES=()
WARNINGS=()

record_fail() {
    fail_check "$1"
    [ -n "${2:-}" ] && info "fix: $2"
    FAILURES+=("$1")
}

record_warn() {
    warn "$1"
    WARNINGS+=("$1")
}

# ----------------------------------------------------------------------------
# Check 1: VPN detection (a VPN that blackholes apt)
# ----------------------------------------------------------------------------

check_vpn() {
    header "Check 1: VPN / tunnel that could blackhole installs"
    local active=""

    # Mullvad
    if command -v mullvad >/dev/null 2>&1; then
        if mullvad status 2>/dev/null | grep -qiE "connected|connecting"; then
            active="$active mullvad"
        fi
    fi

    # NetworkManager VPN connections
    if command -v nmcli >/dev/null 2>&1; then
        local nm_vpns
        nm_vpns=$(nmcli -t -f TYPE,STATE connection show --active 2>/dev/null | grep -iE "vpn|wireguard" | grep -v "potluck" || true)
        if [ -n "$nm_vpns" ]; then
            active="$active networkmanager-vpn"
        fi
    fi

    # Generic WireGuard interfaces (not ours; ours is wg0 from potluck-mesh)
    if command -v wg >/dev/null 2>&1; then
        local wg_ifs
        wg_ifs=$(wg show interfaces 2>/dev/null || true)
        # Filter out our own mesh-managed wg0; anything else is a foreign VPN.
        for iface in $wg_ifs; do
            if [ "$iface" != "wg0" ] && [ "$iface" != "potluck-mesh" ]; then
                active="$active wireguard:$iface"
            fi
        done
    fi

    if [ -z "$active" ]; then
        ok "no active VPN detected"
        return 0
    fi

    if [ "$AUTO_FIX" = "1" ] && echo "$active" | grep -q mullvad; then
        info "AUTO-FIX: disconnecting Mullvad"
        mullvad disconnect 2>/dev/null || true
        sleep 2
        if mullvad status 2>/dev/null | grep -qi "disconnected"; then
            ok "mullvad disconnected"
            return 0
        fi
        record_warn "could not auto-disconnect Mullvad; please run 'mullvad disconnect' manually"
        return 0
    fi

    record_warn "active VPN(s) detected:$active. Installs may hang on apt/pip/npm if the VPN blackholes those endpoints. Consider disconnecting before installing."
    return 0
}

# ----------------------------------------------------------------------------
# Check 2: Python 3.11+ availability
# ----------------------------------------------------------------------------

check_python() {
    header "Check 2: Python 3.11+"
    local found=""
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null; then
                found="$candidate"
                break
            fi
        fi
    done
    if [ -n "$found" ]; then
        ok "using $found ($("$found" --version 2>&1))"
        return 0
    fi

    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "${ID:-}" in
            ubuntu|debian)
                record_fail "Python 3.11+ not found" \
                    "On Ubuntu 22.04 the system ships Python 3.10 which is too old. Install 3.11 via deadsnakes: 'sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt update && sudo apt install -y python3.11 python3.11-venv python3.11-dev'. The bootstrap-linux.sh script automates this."
                ;;
            fedora|rhel|centos|rocky|almalinux)
                record_fail "Python 3.11+ not found" \
                    "On Fedora/RHEL: 'sudo dnf install -y python3.11'"
                ;;
            arch|manjaro)
                record_fail "Python 3.11+ not found" \
                    "On Arch: 'sudo pacman -S python' (default 'python' package is current)"
                ;;
            *)
                record_fail "Python 3.11+ not found" "install Python 3.11+ via your distro's package manager"
                ;;
        esac
    else
        record_fail "Python 3.11+ not found" "install Python 3.11+ via your distro's package manager"
    fi
    return 1
}

# ----------------------------------------------------------------------------
# Check 3: sudo
# ----------------------------------------------------------------------------

check_sudo() {
    header "Check 3: sudo available (needed for mesh sidecar TUN creation)"
    if ! command -v sudo >/dev/null 2>&1; then
        record_fail "sudo not found" \
            "install sudo and add your user: 'apt install sudo && usermod -aG sudo \$USER'. Without sudo the mesh sidecar can't create the TUN interface."
        return 1
    fi
    ok "sudo present"
    return 0
}

# ----------------------------------------------------------------------------
# Check 4: Network endpoints
# ----------------------------------------------------------------------------

probe() {
    local url="$1"
    curl -fsI --max-time 6 "$url" >/dev/null 2>&1
}

check_network() {
    header "Check 4: Network endpoints (PyPI, GitHub, npm)"
    local all_ok=1
    local endpoints=(
        "https://pypi.org/simple/:PyPI"
        "https://github.com/:GitHub"
        "https://registry.npmjs.org/:npm registry"
    )
    for entry in "${endpoints[@]}"; do
        local url="${entry%%:*}"
        local name="${entry##*:}"
        if probe "$url"; then
            ok "$name reachable"
        else
            record_warn "$name unreachable at $url"
            all_ok=0
        fi
    done
    if [ "$all_ok" = "0" ]; then
        info "VPN, DNS, or proxy issue is the usual cause. The Check 1 results above are usually the root cause."
    fi
    return 0
}

# ----------------------------------------------------------------------------
# Check 5: Coordinator reachable
# ----------------------------------------------------------------------------

check_coordinator() {
    header "Check 5: Laptop coordinator reachable"
    if [ -z "$COORDINATOR_URL" ]; then
        record_warn "no --coordinator-url provided; skipping. You'll be prompted later by the main installer."
        return 0
    fi
    local health_url="${COORDINATOR_URL%/}/health"
    if probe "$health_url"; then
        ok "coordinator alive at $health_url"
        return 0
    fi
    record_fail "cannot reach coordinator at $health_url" \
        "1) Confirm 'make up' is running on your laptop. 2) Verify the laptop LAN IP with 'ipconfig getifaddr en0' (Mac) or 'ip -4 addr show' (Linux). 3) Check the laptop's firewall for inbound port 8400."
    return 1
}

# ----------------------------------------------------------------------------
# Check 6: Disk space
# ----------------------------------------------------------------------------

check_disk() {
    header "Check 6: Free disk space"
    local free_kb
    free_kb=$(df -k "$HOME" | awk 'NR==2 {print $4}')
    local free_gb=$(( free_kb / 1024 / 1024 ))
    if [ "$free_gb" -ge 8 ]; then
        ok "${free_gb} GB free in \$HOME (need >= 6 GB)"
        return 0
    fi
    if [ "$free_gb" -ge 6 ]; then
        record_warn "${free_gb} GB free in \$HOME (recommended >= 8 GB; tight)"
        return 0
    fi
    record_fail "only ${free_gb} GB free in \$HOME (need at least 6 GB)" \
        "free disk space or run from a partition with more headroom"
    return 1
}

# ----------------------------------------------------------------------------
# Check 7: Display detection (informational)
# ----------------------------------------------------------------------------

check_display() {
    header "Check 7: Display environment (informational)"
    if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
        ok "graphical session detected ($([ -n "${WAYLAND_DISPLAY:-}" ] && echo Wayland || echo X11)); setup-household.sh will build the Electron desktop app"
    else
        ok "no graphical session detected; setup-household.sh will run in headless / sidecar-only mode"
    fi
    return 0
}

# ----------------------------------------------------------------------------
# Run all checks
# ----------------------------------------------------------------------------

printf '\n'
printf '%s============================================================%s\n' "$C_CYAN" "$C_OFF"
printf '%s Potluck household contributor - Linux pre-flight checks    %s\n' "$C_CYAN" "$C_OFF"
printf '%s============================================================%s\n' "$C_CYAN" "$C_OFF"

check_vpn || true
check_python || true
check_sudo || true
check_network || true
check_coordinator || true
check_disk || true
check_display || true

printf '\n'
printf '%s============================================================%s\n' "$C_CYAN" "$C_OFF"

if [ ${#FAILURES[@]} -gt 0 ]; then
    printf '%s %d BLOCKING issue(s) - fix and re-run preflight %s\n' "$C_RED" "${#FAILURES[@]}" "$C_OFF"
    printf '%s============================================================%s\n\n' "$C_CYAN" "$C_OFF"
    for f in "${FAILURES[@]}"; do
        printf '  - %s\n' "$f"
    done
    printf '\n  After fixing, re-run:  ./scripts/setup-household-preflight.sh\n'
    printf '  Add --auto-fix to auto-fix safe things (Mullvad disconnect)\n\n'
    exit 1
fi

if [ ${#WARNINGS[@]} -gt 0 ]; then
    printf '%s all checks passed (%d warnings) %s\n' "$C_YELLOW" "${#WARNINGS[@]}" "$C_OFF"
    printf '%s============================================================%s\n\n' "$C_CYAN" "$C_OFF"
    for w in "${WARNINGS[@]}"; do
        printf '  - %s\n' "$w"
    done
    printf '\n  Safe to continue. Run:  ./scripts/setup-household.sh\n\n'
    exit 0
fi

printf '%s ALL CHECKS PASSED %s\n' "$C_GREEN" "$C_OFF"
printf '%s============================================================%s\n\n' "$C_CYAN" "$C_OFF"
printf '  Ready to install. Run:  ./scripts/setup-household.sh\n\n'
exit 0
