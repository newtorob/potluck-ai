#!/usr/bin/env bash
# Post-deploy smoke test against LIVE production Potluck infra.
#
# WHY THIS EXISTS: the coordinator has two independent auth gates —
# require_coordinator_token (a shared secret only the Python sidecar holds)
# and resolve_session_member_id (the real per-member session gate). The
# desktop frontend calls a set of coordinator routes DIRECTLY from the
# Electron renderer and can never hold the coordinator's shared secret, so
# those routes must depend ONLY on the session gate. We shipped a prod
# incident where several frontend-direct routes (/me/circles,
# /me/credit-balance, /me/contributions, /jobs/stream, /jobs/stats, etc.)
# still carried the token dependency: it was invisible in dev (the token
# gate no-ops when POTLUCK_COORDINATOR_TOKEN is unset) and only broke the
# instant prod set a real token.
#
# IMPORTANT — the two gates are NOT distinguishable from an unauthenticated
# probe: require_coordinator_token ALSO raises 401 (middleware/auth.py:51),
# exactly like the session gate. So check 3 (no token, no session) can only
# prove a route "requires auth"; a 401 there does NOT prove the token gate is
# gone. The DEFINITIVE regression guard for this bug is check 6
# (--with-session): a route that still carries the token dependency will 401
# even for a VALID session (token gate fires first, before the session is
# ever read), whereas a correctly session-only route returns 2xx. Run this
# WITH a session after every coordinator deploy to actually confirm the fix.
# See services/coordinator/tests/test_prod_auth_posture.py for the matching
# unit-level guardrail (which CAN set both gates and assert directly).
#
# NOT WIRED TO CI (cost/simplicity — this is a solo-operator shop). This is a
# manual "make smoke-prod" run after every coordinator/api deploy, and a
# standing item on docs/launch/production-launch-checklist.md.
#
# Default run is READ-ONLY / SAFE: no emails sent, no accounts created, no
# mutation of prod state. Everything below check 3's negative-auth probes and
# the CORS preflight is a GET or an intentionally-pre-auth POST that the
# coordinator rejects at the auth layer before touching the DB.
#
# Usage:
#   scripts/smoke_prod.sh                                   # safe checks only (default)
#   scripts/smoke_prod.sh --with-session TOKEN               # also prove a real session
#                                                              # gets 2xx on the same routes
#                                                              # with NO coordinator token
#                                                              # (env: POTLUCK_SMOKE_SESSION_TOKEN)
#   scripts/smoke_prod.sh --send-email you@example.com       # ALSO fire a real magic-link
#                                                              # email via POST /auth/request-link
#                                                              # (sends real email -- opt in only)
#
# Env overrides (defaults are prod):
#   API_URL          default https://api.trypotluck.ai
#   COORDINATOR_URL  default https://coordinator.trypotluck.ai
#
# Exit code: 0 if every REQUIRED check passes, 1 otherwise. Opt-in checks
# (--with-session, --send-email) count toward the total only when requested.
#
# bash 3.2 compatible (macOS default -- no associative arrays).
set -uo pipefail

API_URL="${API_URL:-https://api.trypotluck.ai}"
COORDINATOR_URL="${COORDINATOR_URL:-https://coordinator.trypotluck.ai}"

WITH_SESSION_TOKEN="${POTLUCK_SMOKE_SESSION_TOKEN:-}"
SEND_EMAIL_ADDR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --with-session)
      WITH_SESSION_TOKEN="${2:-}"
      shift 2
      ;;
    --send-email)
      SEND_EMAIL_ADDR="${2:-}"
      shift 2
      ;;
    -h|--help)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

PASS=0
FAIL=0
TOTAL=0

# check_result NAME STATUS PASS_CONDITION_DESCRIPTION OK
# OK is "1" for pass, "0" for fail.
report() {
  name="$1"; got="$2"; note="$3"; ok="$4"
  TOTAL=$((TOTAL + 1))
  if [ "$ok" = "1" ]; then
    PASS=$((PASS + 1))
    printf "  \033[32m\xe2\x9c\x93\033[0m %-58s got=%-5s %s\n" "$name" "$got" "$note"
  else
    FAIL=$((FAIL + 1))
    printf "  \033[31m\xe2\x9c\x97\033[0m %-58s got=%-5s %s\n" "$name" "$got" "$note"
  fi
}

http_code() {
  # http_code METHOD URL [extra curl args...]
  method="$1"; url="$2"; shift 2
  curl -s -o /dev/null -w "%{http_code}" --max-time 10 -X "$method" "$url" "$@" 2>/dev/null
}

echo "Potluck prod smoke test"
echo "  API_URL:         $API_URL"
echo "  COORDINATOR_URL: $COORDINATOR_URL"
echo

# ---------------------------------------------------------------------------
# 1. API health -- services/api/potluck_api/routers/health.py:12-24, mounted
#    with no prefix at services/api/potluck_api/app.py:92 -> GET /health
# ---------------------------------------------------------------------------
echo "== 1. Cloud API health =="
code=$(http_code GET "$API_URL/health")
[ "$code" = "200" ] && ok=1 || ok=0
report "GET /health" "$code" "(expect 200)" "$ok"
echo

# ---------------------------------------------------------------------------
# 2. Coordinator health -- services/coordinator/coordinator/routers/health.py:12-21,
#    mounted with no prefix at services/coordinator/coordinator/app.py:138 -> GET /health
# ---------------------------------------------------------------------------
echo "== 2. Coordinator health =="
code=$(http_code GET "$COORDINATOR_URL/health")
[ "$code" = "200" ] && ok=1 || ok=0
report "GET /health" "$code" "(expect 200)" "$ok"
echo

# ---------------------------------------------------------------------------
# 3. Auth-posture BASELINE -- necessary but NOT sufficient. Frontend-direct
#    routes (services/coordinator/tests/test_prod_auth_posture.py
#    FRONTEND_DIRECT_ROUTES, lines 97-113) called with NO coordinator token and
#    NO session must require auth. NOTE: both gates raise 401, so a 401 here
#    only proves "the route requires auth" -- it does NOT prove the token gate
#    was dropped. Check 6 (--with-session) is what actually confirms the fix.
#      401 = route requires auth (baseline OK; gate identity unknown from here)
#      200 = auth is off entirely (BUG)
#      403 = some other authz failure (unexpected -- investigate)
#      5xx = broken route
# ---------------------------------------------------------------------------
echo "== 3. Coordinator auth posture BASELINE (no token, no session) =="
echo "   note: 401 here only proves 'requires auth'; run --with-session (check 6)"
echo "   to confirm the token gate is actually gone."
check_401() {
  # check_401 LABEL METHOD PATH [curl-body-args...]
  label="$1"; method="$2"; path="$3"; shift 3
  code=$(http_code "$method" "$COORDINATOR_URL$path" "$@")
  note=""
  ok=0
  case "$code" in
    401) ok=1; note="(requires auth -- baseline OK; use check 6 to prove session-only)" ;;
    200|201|202|204) ok=0; note="(BUG: route is not gated at all)" ;;
    403) ok=0; note="(unexpected authz failure -- investigate)" ;;
    *) ok=0; note="(unexpected -- broken route or infra issue)" ;;
  esac
  report "$label" "$code" "$note" "$ok"
}

check_401 "GET /me/circles"           GET  "/me/circles"
check_401 "GET /me/credit-balance"    GET  "/me/credit-balance"
check_401 "GET /me/contributions"     GET  "/me/contributions"
check_401 "POST /jobs/stream"         POST "/jobs/stream" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"smoke test probe","model_slug":"stub-echo"}'
check_401 "GET /jobs/stats"           GET  "/jobs/stats"
echo

# ---------------------------------------------------------------------------
# 4. CORS preflight -- packaged Electron app origin must be allowlisted.
#    services/coordinator/coordinator/app.py:23 _BASE_CORS_ORIGINS =
#    ["app://.", "file://"] (always-on, every env). Probe with Origin: file://
#    against a frontend-direct route, OPTIONS preflight.
# ---------------------------------------------------------------------------
echo "== 4. CORS preflight (packaged app origin) =="
cors_headers=$(curl -s -D - -o /dev/null --max-time 10 \
  -X OPTIONS "$COORDINATOR_URL/me/circles" \
  -H "Origin: file://" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: content-type" 2>/dev/null)
cors_status=$(printf '%s' "$cors_headers" | head -1 | tr -d '\r' | awk '{print $2}')
allow_origin=$(printf '%s' "$cors_headers" | grep -i '^access-control-allow-origin:' | tr -d '\r' | awk '{print $2}')
if [ "$allow_origin" = "file://" ] || [ "$allow_origin" = "*" ]; then
  ok=1; note="(allow-origin: ${allow_origin:-<none>})"
else
  ok=0; note="(allow-origin: ${allow_origin:-<MISSING> -- packaged app cannot call coordinator}, status=$cors_status)"
fi
report "OPTIONS /me/circles (Origin: file://)" "$cors_status" "$note" "$ok"
echo

# ---------------------------------------------------------------------------
# 5. Models catalog feed -- services/api/potluck_api/routers/models.py:58-69,
#    mounted at prefix /models (services/api/potluck_api/app.py:109) ->
#    GET /models/catalog. Verify 200 + JSON with schema_version present.
# ---------------------------------------------------------------------------
echo "== 5. Models catalog feed =="
body=$(curl -s --max-time 10 "$API_URL/models/catalog" 2>/dev/null)
code=$(http_code GET "$API_URL/models/catalog")
schema_version=$(printf '%s' "$body" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    v = d.get('schema_version')
    print(v if v is not None else '')
except Exception:
    print('')
" 2>/dev/null)
if [ "$code" = "200" ] && [ -n "$schema_version" ]; then
  ok=1; note="(schema_version=$schema_version)"
else
  ok=0; note="(schema_version missing or invalid JSON)"
fi
report "GET /models/catalog" "$code" "$note" "$ok"
echo

# ---------------------------------------------------------------------------
# OPT-IN 6. --with-session: THE DEFINITIVE regression check. Prove the
# frontend-direct routes return 2xx WITH a real session bearer and NO
# coordinator token. A route that still carries require_coordinator_token
# 401s here even for a valid session (the token gate fires before the session
# is read) -- so a 401 in THIS check is the exact prod bug, caught for real.
# ---------------------------------------------------------------------------
if [ -n "$WITH_SESSION_TOKEN" ]; then
  echo "== 6. [opt-in] DEFINITIVE: frontend-direct routes WITH session (no coordinator token) =="
  check_2xx_session() {
    label="$1"; method="$2"; path="$3"; shift 3
    code=$(http_code "$method" "$COORDINATOR_URL$path" \
      -H "Authorization: Bearer $WITH_SESSION_TOKEN" "$@")
    case "$code" in
      2??) ok=1; note="(session credential accepted end to end)" ;;
      401) ok=0; note="(BUG: valid session 401'd -- token gate still on this route)" ;;
      403) ok=0; note="(BUG: valid session rejected)" ;;
      *) ok=0; note="(unexpected -- domain-level failure or broken route)" ;;
    esac
    report "$label" "$code" "$note" "$ok"
  }
  check_2xx_session "GET /me/circles"        GET  "/me/circles"
  check_2xx_session "GET /me/credit-balance" GET  "/me/credit-balance"
  check_2xx_session "GET /me/contributions"  GET  "/me/contributions"
  check_2xx_session "GET /jobs/stats"        GET  "/jobs/stats"
  echo
else
  echo "== 6. [opt-in] Skipped -- pass --with-session TOKEN or set POTLUCK_SMOKE_SESSION_TOKEN =="
  echo
fi

# ---------------------------------------------------------------------------
# OPT-IN 7. --send-email: fire a real magic-link email.
#    services/api/potluck_api/routers/auth.py:57-91 -> POST /auth/request-link,
#    body {"email": "..."}, expect 202 Accepted (always 202 regardless of
#    whether the address maps to a member -- does not leak account existence).
# ---------------------------------------------------------------------------
if [ -n "$SEND_EMAIL_ADDR" ]; then
  echo "== 7. [opt-in] Magic-link send -- REAL EMAIL to $SEND_EMAIL_ADDR =="
  code=$(http_code POST "$API_URL/auth/request-link" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$SEND_EMAIL_ADDR\"}")
  [ "$code" = "202" ] && ok=1 || ok=0
  report "POST /auth/request-link" "$code" "(expect 202; check inbox at $SEND_EMAIL_ADDR)" "$ok"
  echo
else
  echo "== 7. [opt-in] Skipped -- pass --send-email you@example.com to test the real send path =="
  echo
fi

echo "======================================================================"
if [ "$FAIL" -eq 0 ]; then
  printf "\033[32mPASS\033[0m -- %d/%d checks passed\n" "$PASS" "$TOTAL"
  exit 0
else
  printf "\033[31mFAIL\033[0m -- %d/%d checks passed (%d failed)\n" "$PASS" "$TOTAL" "$FAIL"
  exit 1
fi
