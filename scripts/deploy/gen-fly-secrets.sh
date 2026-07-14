#!/usr/bin/env bash
# Mint the strong secrets the Fly deploy needs and print ready-to-run
# `fly secrets set` commands. Nothing is stored or transmitted — the values
# print to your terminal once. Copy them into a password manager, then run the
# emitted commands (or pipe the STDIN-safe form). See docs/launch/fly-deploy.md.
#
# Usage:
#   ./scripts/deploy/gen-fly-secrets.sh            # print values + commands
#   ./scripts/deploy/gen-fly-secrets.sh --raw      # print only the raw values
set -euo pipefail

# 64 hex chars (32 bytes). Used for both HMAC secrets and the shared tokens;
# comfortably above every length floor the code enforces.
gen() { openssl rand -hex 32; }

MAGIC_LINK_SECRET="$(gen)"
DISCOURSE_SSO_SECRET="$(gen)"
POTLUCK_COORDINATOR_TOKEN="$(gen)"
POTLUCK_DERP_JWT_KEY="$(gen)"

if [[ "${1:-}" == "--raw" ]]; then
  cat <<EOF
MAGIC_LINK_SECRET=$MAGIC_LINK_SECRET
DISCOURSE_SSO_SECRET=$DISCOURSE_SSO_SECRET
POTLUCK_COORDINATOR_TOKEN=$POTLUCK_COORDINATOR_TOKEN
POTLUCK_DERP_JWT_KEY=$POTLUCK_DERP_JWT_KEY
EOF
  exit 0
fi

cat <<EOF
# ---------------------------------------------------------------------------
# Generated secrets. Save these in a password manager NOW — they are shown once.
# The DERP JWT key MUST be identical on the coordinator and the DERP relay.
# ---------------------------------------------------------------------------

# ---- potluck-api ----
fly secrets set --app potluck-api \\
  MAGIC_LINK_SECRET=$MAGIC_LINK_SECRET \\
  DISCOURSE_SSO_SECRET=$DISCOURSE_SSO_SECRET

# ---- potluck-coordinator ----
fly secrets set --app potluck-coordinator \\
  POTLUCK_COORDINATOR_TOKEN=$POTLUCK_COORDINATOR_TOKEN \\
  POTLUCK_DERP_JWT_KEY=$POTLUCK_DERP_JWT_KEY

# ---- potluck-derp (same DERP key as the coordinator) ----
fly secrets set --app potluck-derp \\
  POTLUCK_DERP_JWT_KEY=$POTLUCK_DERP_JWT_KEY

# Still needed, from your accounts (not generated here): DATABASE_URL (per app,
# from the Postgres attach), RESEND_API_KEY, STRIPE_* , and optionally SENTRY_DSN.
# See docs/launch/fly-deploy.md for the full per-app secret list.
EOF
