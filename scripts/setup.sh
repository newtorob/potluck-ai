#!/usr/bin/env bash
#
# scripts/setup.sh — the friendly entrypoint.
#
# Asks what you're trying to do and runs the right script for you. If you
# already know what you want, run that script directly:
#
#   ./scripts/setup-dev.sh         # set up to develop Potluck itself
#   ./scripts/setup-household.sh   # set up this machine as a household contributor

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ -t 1 ]; then
    C_CYAN=$'\033[36m'
    C_GREEN=$'\033[32m'
    C_DIM=$'\033[2m'
    C_OFF=$'\033[0m'
else
    C_CYAN=""; C_GREEN=""; C_DIM=""; C_OFF=""
fi

cat <<EOF

${C_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_OFF}
${C_CYAN} Potluck setup ${C_OFF}
${C_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_OFF}

What are you trying to do?

  ${C_GREEN}1)${C_OFF} Develop Potluck itself
     ${C_DIM}(you'll edit code, run tests, etc.)${C_OFF}

  ${C_GREEN}2)${C_OFF} Set up this Linux machine as a household contributor
     ${C_DIM}(let your laptop's Potluck app use this machine's CPU/GPU
     for inference, over your private mesh)${C_OFF}

  ${C_GREEN}3)${C_OFF} Check what's running on this machine
     ${C_DIM}(status of mesh + Python sidecars, if any)${C_OFF}

  ${C_GREEN}4)${C_OFF} Stop everything Potluck is running
     ${C_DIM}(both sidecars; doesn't touch the desktop app)${C_OFF}

  ${C_GREEN}q)${C_OFF} Quit

EOF

read -r -p "  Choice [1/2/3/4/q]: " choice
echo

case "${choice,,}" in
    1)
        exec "$REPO_ROOT/scripts/setup-dev.sh"
        ;;
    2)
        case "$(uname -s)" in
            Linux)
                exec "$REPO_ROOT/scripts/setup-household.sh"
                ;;
            *)
                cat <<EOF
${C_DIM}On Linux: run ./scripts/setup-household.sh (this script just dispatches to it).
On Windows: open PowerShell in the repo root and run
  .\\scripts\\setup-household.ps1
On Mac: a household-contributor script isn't shipped yet; for now, use
'make desktop' to run the app, then pair another machine in via the
Household view's "Add another machine" button.${C_OFF}
EOF
                exit 1
                ;;
        esac
        ;;
    3)
        if [ -x "$REPO_ROOT/scripts/setup-household.sh" ]; then
            exec "$REPO_ROOT/scripts/setup-household.sh" --status
        fi
        echo "Nothing to check on this platform yet."
        ;;
    4)
        if [ -x "$REPO_ROOT/scripts/setup-household.sh" ]; then
            exec "$REPO_ROOT/scripts/setup-household.sh" --stop
        fi
        echo "Nothing to stop on this platform yet."
        ;;
    q|quit|exit)
        exit 0
        ;;
    *)
        echo "  Unknown choice: $choice"
        exit 1
        ;;
esac
