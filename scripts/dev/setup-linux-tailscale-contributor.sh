#!/bin/bash
# One-shot Linux bootstrap for the Potluck contributor sidecar (Phase G m1).
# This is the TAILSCALE variant — for a Linux box that reaches the dev Mac
# only via Tailscale (different network, VPS, behind CGNAT, etc.). For a Linux
# box on the same LAN, use setup-linux-contributor.sh instead.
#
# What it does:
#   1. Checks tailscale + Python 3.11 + git + curl are present
#   2. Verifies the coordinator is reachable on the Mac's Tailscale IP
#   3. Runs scripts/dev/install-contributor.py
#   4. Writes apps/desktop/python/.env with the dev smoke-test values
#   5. Installs a systemd user unit so the sidecar runs across reboots
#
# Idempotent — safe to re-run.

set -euo pipefail

# ---------- HARDCODED CONFIG (edit if tailnet IP / keys change) ---------------

COORDINATOR_URL="ws://100.101.102.103:8400/peers/connect"
COORDINATOR_HTTP="http://100.101.102.103:8400"
BINARY_HASH="59b74f2ea0e8b6f23f3672336f90c97435b1dfa13ef7aabc1994b61c54c7d65a"
MEMBER_ID="00000000-0000-0000-0000-000000000001"

# ---------- helpers -----------------------------------------------------------

C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YELLOW=$'\033[33m'; C_OFF=$'\033[0m'
step() { printf "%s==> %s%s\n" "$C_CYAN" "$1" "$C_OFF"; }
pass() { printf "    %s[ok]   %s%s\n" "$C_GREEN" "$1" "$C_OFF"; }
warn() { printf "    %s[warn] %s%s\n" "$C_YELLOW" "$1" "$C_OFF"; }
fail() { printf "    %s[fail] %s%s\n" "$C_RED" "$1" "$C_OFF"; exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SIDECAR_DIR="$REPO_ROOT/apps/desktop/python"
VENV_PY="$SIDECAR_DIR/.venv/bin/python"
ENV_FILE="$SIDECAR_DIR/.env"

# ---------- 1. Tailscale ------------------------------------------------------

step "Checking Tailscale"

# Need curl + sudo for the install path. We check curl in step 2 too, but for
# the install branch we need it earlier.
if ! command -v tailscale >/dev/null; then
    if ! command -v curl >/dev/null; then
        fail "tailscale missing AND curl missing. Install curl first ('apt install curl'), then re-run."
    fi
    if ! command -v sudo >/dev/null; then
        fail "tailscale missing and 'sudo' not available. Install Tailscale manually per https://tailscale.com/download/linux."
    fi
    warn "tailscale not installed — running the official install script (requires sudo)"
    if ! curl -fsSL https://tailscale.com/install.sh | sudo sh; then
        fail "tailscale install failed. Try the manual steps at https://tailscale.com/download/linux."
    fi
    if ! command -v tailscale >/dev/null; then
        fail "tailscale install reported success but the CLI still isn't on PATH. Open a new shell and re-run."
    fi
    pass "tailscale CLI installed"
fi

# CLI is present; now check it's actually up + signed in.
if ! tailscale status >/dev/null 2>&1; then
    warn "tailscale is installed but not connected — running 'sudo tailscale up'"
    cat <<EOF
    A browser-auth URL will print below. Copy it, paste it into a browser
    on any device, and sign in to the SAME Tailscale account that your
    dev Mac uses (Google / Microsoft / GitHub / etc. — must match the
    provider, not just the email). When auth completes, this script
    continues automatically.

EOF
    if ! sudo tailscale up; then
        fail "'sudo tailscale up' failed. Check the output above and re-run."
    fi
    if ! tailscale status >/dev/null 2>&1; then
        fail "tailscale still not up after 'tailscale up'. Run 'sudo tailscale up' manually and re-run this script."
    fi
fi
pass "tailscale is up"

# ---------- 2. Other prereqs --------------------------------------------------

step "Checking prerequisites"
command -v git >/dev/null  || fail "git missing — apt install git"
command -v curl >/dev/null || fail "curl missing — apt install curl"

PY=""
for c in python3.11 python3 python; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2]==(3,11) else 1)' 2>/dev/null; then
        PY="$c"; break
    fi
done
[ -n "$PY" ] || fail "Python 3.11 missing. Ubuntu/Debian: 'sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.11 python3.11-venv python3.11-dev'"
pass "found Python 3.11 ($PY)"
pass "git + curl present"

# ---------- 3. Reachability ---------------------------------------------------

step "Verifying coordinator reachability over Tailscale ($COORDINATOR_HTTP)"
if curl -fsS --max-time 5 "$COORDINATOR_HTTP/transparency/fleet" >/dev/null 2>&1; then
    pass "coordinator is reachable"
else
    cat <<EOF
    ${C_RED}[fail]${C_OFF} Coordinator unreachable over Tailscale.

    Common causes:
      1. Mac isn't on the same tailnet (different account). Check with
         'tailscale status' here vs. on the Mac.
      2. Mac's Tailscale IP changed. Get the new one on the Mac with
         'tailscale ip -4' and edit COORDINATOR_URL/HTTP at the top of
         this script.
      3. Coordinator stopped on the Mac: 'docker ps' on the Mac.
      4. Mullvad/other VPN killswitch blackholing tailnet traffic on this
         box. Disconnect Mullvad and retry, or configure split-tunnel.

EOF
    exit 1
fi

# ---------- 4. Install --------------------------------------------------------

step "Running scripts/dev/install-contributor.py"
"$PY" "$REPO_ROOT/scripts/dev/install-contributor.py"
[ -x "$VENV_PY" ] || fail "venv python missing at $VENV_PY after install"
pass "venv ready at $VENV_PY"

# ---------- 5. .env -----------------------------------------------------------

step "Writing $ENV_FILE"
cat > "$ENV_FILE" <<EOF
# Generated by scripts/setup-linux-tailscale-contributor.sh — do not commit.
POTLUCK_COORDINATOR_URL=$COORDINATOR_URL
POTLUCK_BINARY_HASH=$BINARY_HASH
POTLUCK_MEMBER_ID=$MEMBER_ID
EOF
pass "wrote .env"

# ---------- 6. Smoke-launch ---------------------------------------------------

step "Smoke-launching sidecar (15s, then auto-kill)"
SMOKE_LOG="$(mktemp)"
( cd "$SIDECAR_DIR" && timeout 15 "$VENV_PY" -m potluck_local.contributor --verbose >"$SMOKE_LOG" 2>&1 || true )
if grep -q "session_opened" "$SMOKE_LOG"; then
    pass "session_opened observed — handshake worked"
elif grep -qE "binary_hash_mismatch|not in contributor_releases" "$SMOKE_LOG"; then
    fail "coordinator rejected binary_hash. Either .env didn't load or seed isn't in contributor_releases."
elif grep -q "missing POTLUCK_BINARY_HASH" "$SMOKE_LOG"; then
    fail ".env didn't load — check $ENV_FILE has the 3 vars."
else
    warn "didn't see session_opened in 15s. Last 10 lines:"
    tail -n 10 "$SMOKE_LOG" | sed 's/^/    /'
    warn "If the lines look healthy, try the systemd service below and watch journalctl."
fi
rm -f "$SMOKE_LOG"

# ---------- 7. systemd user unit ---------------------------------------------

step "Installing systemd user unit (auto-start across reboots)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/potluck-contributor.service" <<EOF
[Unit]
Description=Potluck contributor sidecar (Phase G m1, Tailscale)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SIDECAR_DIR
ExecStart=$VENV_PY -m potluck_local.contributor
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now potluck-contributor.service
sleep 2
if systemctl --user is-active --quiet potluck-contributor.service; then
    pass "systemd user service is active"
else
    warn "service didn't come up cleanly — check 'journalctl --user -u potluck-contributor -n 30'"
fi

loginctl enable-linger "$USER" 2>/dev/null || warn "couldn't enable-linger (may need sudo); service won't start before first login"

cat <<EOF

${C_CYAN}==> Setup complete${C_OFF}

Status:    systemctl --user status potluck-contributor
Logs:      journalctl --user -u potluck-contributor -f
Stop:      systemctl --user stop potluck-contributor
Disable:   systemctl --user disable --now potluck-contributor

Per-peer Ed25519 key: ~/.local/share/Potluck/contributor/peer-key.pem

EOF
