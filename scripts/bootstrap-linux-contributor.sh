#!/usr/bin/env bash
# Bootstrap an Ubuntu box for the Potluck Phase G m1 contributor sidecar.
# Run with: sudo ./scripts/bootstrap-linux-contributor.sh
#
# Performs (idempotent where possible):
#   1. Install /etc/sudoers.d/99-no-tty-tickets so cached sudo creds work
#      across ttys for the invoking user (needed so the agent can run sudo
#      from non-interactive tool calls).
#   2. Disconnect Mullvad if it's currently connected.
#   3. Add deadsnakes PPA and install python3.11 + venv + dev + git +
#      build-essential.
#   4. Install Tailscale via the official install script.
#
# Does NOT run `tailscale up` — that needs interactive browser auth and
# should be run by hand afterwards.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Must be run as root. Try: sudo $0" >&2
  exit 1
fi

if [[ -z "${SUDO_USER:-}" ]]; then
  echo "SUDO_USER is empty — run via 'sudo', not as the root user directly." >&2
  exit 1
fi

step() { printf '\n=== %s ===\n' "$1"; }

step "install !tty_tickets sudoers fragment for $SUDO_USER"
TMPF=$(mktemp)
trap 'rm -f "$TMPF"' EXIT
printf 'Defaults:%s !tty_tickets\n' "$SUDO_USER" > "$TMPF"
visudo -cf "$TMPF"
install -m 0440 -o root -g root "$TMPF" /etc/sudoers.d/99-no-tty-tickets
ls -la /etc/sudoers.d/99-no-tty-tickets

step "disconnect mullvad (if connected)"
if command -v mullvad >/dev/null 2>&1; then
  if mullvad status 2>/dev/null | grep -qi connected; then
    mullvad disconnect
    sleep 2
  fi
  mullvad status 2>&1 | head -3 || true
else
  echo "mullvad CLI not found — skipping"
fi

step "add deadsnakes PPA"
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -qq

step "install python3.11 + build tools + git"
apt-get install -y \
  python3.11 \
  python3.11-venv \
  python3.11-dev \
  git \
  build-essential \
  curl

step "install tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
else
  echo "tailscale already installed — skipping"
fi

step "versions"
python3.11 --version
tailscale version | head -1

printf '\n=== ALL DONE ===\n'
echo "Next: run 'sudo tailscale up' (browser auth), then come back."
