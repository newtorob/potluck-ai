#!/usr/bin/env bash
#
# bootstrap-linux.sh
#
# OS-level preparation for running Potluck on an Ubuntu/Debian Linux box.
# Run BEFORE setup-household.sh, with sudo:
#
#     sudo ./scripts/bootstrap-linux.sh
#     sudo ./scripts/bootstrap-linux.sh --with-tailscale     # also install Tailscale
#     sudo ./scripts/bootstrap-linux.sh --with-sudo-tickets  # !tty_tickets sudoers fragment (advanced)
#
# Performs (idempotent where possible):
#   1. Disconnect Mullvad if connected. The #1 reason yesterday's installs
#      hung silently was an active VPN blackholing apt/pip endpoints.
#   2. Add deadsnakes PPA (Ubuntu 22.04 + earlier ship Python 3.10; we need 3.11+).
#   3. Install Python 3.11 + venv + dev + git + build-essential + curl + wireguard-tools.
#
# Optional (flag-gated):
#   --with-tailscale       Install the Tailscale CLI. Only useful if you're
#                           NOT using Potluck's built-in WireGuard mesh
#                           (the household contributor flow). Does NOT run
#                           'tailscale up' (needs interactive browser auth).
#   --with-sudo-tickets    Install /etc/sudoers.d/99-no-tty-tickets so sudo
#                           creds persist across ttys for the invoking user.
#                           Useful for agent-driven (Claude Code etc.) installs
#                           where sudo is called from non-interactive tool
#                           calls. Modifying sudoers is invasive enough to
#                           require explicit opt-in.
#
# After bootstrap finishes, run setup-household.sh (or use ./scripts/setup.sh
# which dispatches to it) to install + build the Potluck stack.

set -euo pipefail

# ----------------------------------------------------------------------------
# Parse args
# ----------------------------------------------------------------------------

WITH_TAILSCALE=0
WITH_SUDO_TICKETS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --with-tailscale)    WITH_TAILSCALE=1; shift ;;
    --with-sudo-tickets) WITH_SUDO_TICKETS=1; shift ;;
    -h|--help)           sed -n '2,33p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Must be run as root. Try: sudo $0" >&2
  exit 1
fi

if [[ -z "${SUDO_USER:-}" ]]; then
  echo "SUDO_USER is empty - run via 'sudo', not as the root user directly." >&2
  exit 1
fi

step() { printf '\n=== %s ===\n' "$1"; }

# ----------------------------------------------------------------------------
# Distro check
# ----------------------------------------------------------------------------

if [ ! -r /etc/os-release ]; then
  echo "Cannot read /etc/os-release; this script targets Ubuntu/Debian only." >&2
  exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-} ${ID_LIKE:-}" in
  *debian*|*ubuntu*) ;;
  *)
    echo "This script targets Ubuntu/Debian (apt). On ${ID:-unknown}, install python3.11+, git, build-essential, and curl via your distro's package manager directly, then skip to ./scripts/setup-household.sh." >&2
    exit 1
    ;;
esac

# ----------------------------------------------------------------------------
# 1. VPN disconnect (yesterday's bug)
# ----------------------------------------------------------------------------

step "disconnect Mullvad if connected"
if command -v mullvad >/dev/null 2>&1; then
  if mullvad status 2>/dev/null | grep -qi connected; then
    mullvad disconnect || true
    sleep 2
    echo "  done."
  else
    echo "  not connected; nothing to do."
  fi
else
  echo "  mullvad CLI not present; skipping."
fi

# ----------------------------------------------------------------------------
# 2. deadsnakes PPA + Python 3.11
# ----------------------------------------------------------------------------

step "add deadsnakes PPA (for Python 3.11 on Ubuntu 22.04+)"
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -qq

step "install python3.11 + build tools + git"
apt-get install -y \
  python3.11 \
  python3.11-venv \
  python3.11-dev \
  git \
  build-essential \
  curl \
  pkg-config \
  wireguard-tools

# ----------------------------------------------------------------------------
# 3. Optional: Tailscale
# ----------------------------------------------------------------------------

if [ "$WITH_TAILSCALE" = "1" ]; then
  step "install Tailscale (via official installer)"
  if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
  else
    echo "  already installed; skipping."
  fi
  echo "  NOTE: 'tailscale up' still needs to be run by hand (browser auth)."
else
  echo
  echo "Skipping Tailscale install (pass --with-tailscale to include)."
  echo "Potluck's built-in WireGuard mesh doesn't require Tailscale."
fi

# ----------------------------------------------------------------------------
# 4. Optional: sudoers !tty_tickets fragment
# ----------------------------------------------------------------------------

if [ "$WITH_SUDO_TICKETS" = "1" ]; then
  step "install !tty_tickets sudoers fragment for $SUDO_USER (advanced)"
  TMPF=$(mktemp)
  trap 'rm -f "$TMPF"' EXIT
  printf 'Defaults:%s !tty_tickets\n' "$SUDO_USER" > "$TMPF"
  visudo -cf "$TMPF"
  install -m 0440 -o root -g root "$TMPF" /etc/sudoers.d/99-no-tty-tickets
  ls -la /etc/sudoers.d/99-no-tty-tickets
  echo "  NOTE: this lets sudo creds persist across ttys for $SUDO_USER. Useful for agent-driven setups; remove with 'sudo rm /etc/sudoers.d/99-no-tty-tickets' to revert."
else
  echo
  echo "Skipping !tty_tickets sudoers fragment (pass --with-sudo-tickets to include)."
fi

# ----------------------------------------------------------------------------
# Versions + next step
# ----------------------------------------------------------------------------

step "installed versions"
python3.11 --version
if command -v tailscale >/dev/null 2>&1; then tailscale version | head -1; fi
git --version

printf '\n=== ALL DONE ===\n'
echo
echo "Next steps (as your regular user, NOT root):"
echo "  cd $(pwd)"
echo "  ./scripts/setup-household-preflight.sh    # verify environment"
echo "  ./scripts/setup-household.sh              # install + build Potluck"
echo
if [ "$WITH_TAILSCALE" = "1" ]; then
  echo "If you intend to use Tailscale for off-LAN reach to your laptop's coordinator:"
  echo "  sudo tailscale up                       # browser auth, sign in to your tailnet"
fi
