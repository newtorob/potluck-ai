#!/usr/bin/env bash
# Helpers for run.sh: container plumbing, small queries, and the assertions.
# Written for bash 3.2 (macOS /bin/bash): no associative arrays, no mapfile.
# An assertion that fails calls fail(), which names the phase and step, dumps
# some state and exits 1. Never call an assertion inside $( ).

PROJECT=meshlive
IPA=; IPB=; IPC=          # mesh addresses, set once a node is paired

dc()   { docker compose -p "$PROJECT" -f "$H/docker-compose.yml" --profile fake "$@"; }
# ex NODE 'shell command' : run it inside a container (stdin closed)
ex()   { local n=$1; shift; dc exec -T "$n" sh -c "$*" </dev/null; }
# J ARGS : jq, run in a container so the host needs no jq. Reads stdin.
J()    { dc exec -T stun jq "$@"; }
# api NODE METHOD PATH [json] : the sidecar's local API over its unix socket
api() {
  local n=$1 m=$2 p=$3 b=${4:-}
  if [ -n "$b" ]; then
    dc exec -T "$n" curl -s -m 30 --unix-socket /run/potluck-mesh.sock -X "$m" -H 'Content-Type: application/json' -d "$b" "http://localhost$p" </dev/null
  else
    dc exec -T "$n" curl -s -m 30 --unix-socket /run/potluck-mesh.sock -X "$m" "http://localhost$p" </dev/null
  fi
}
# coord METHOD PATH [json] : the coordinator, over the container network, from node-a
coord() {
  local m=$1 p=$2 b=${3:-}
  if [ -n "$b" ]; then
    dc exec -T node-a curl -s -m 20 -X "$m" -H 'Content-Type: application/json' -d "$b" "http://coordinator:8400$p" </dev/null
  else
    dc exec -T node-a curl -s -m 20 -X "$m" "http://coordinator:8400$p" </dev/null
  fi
}
psql() { dc exec -T postgres psql -U potluck -d potluck -v ON_ERROR_STOP=1 -P pager=off "$@" </dev/null; }
# set_ip NODE VALUE : change a node's mesh_ip row in the coordinator's database
set_ip() { psql -t -A -c "update mesh_nodes set mesh_ip='$2' where node_name='$1' returning 'row now: '||mesh_ip" 2>&1; }
# trim: cut any 44-char base64 key to 6 chars so keys never fill the transcript
trim() { sed -E 's/([A-Za-z0-9+\/]{6})[A-Za-z0-9+\/]{37}=/\1../g'; }

# ---------------------------------------------------------------- reporting
CUR_PHASE=-; CUR_STEP=-; PASSES=0; PHASE_PASSES=0; PHASE_T0=0; DONE=
say()  { printf '\n\n########## %s\n' "$*"; }
note() { printf '# %s\n' "$*"; }
phase() { CUR_PHASE=$1; CUR_STEP=-; PHASE_PASSES=0; PHASE_T0=$SECONDS; say "$1. $2"; }
phase_end() {
  DONE="$DONE $CUR_PHASE"
  printf '\nPHASE %s PASSED (%s assertions, %ss)\n' "$CUR_PHASE" "$PHASE_PASSES" "$((SECONDS-PHASE_T0))"
  SUMMARY="${SUMMARY:-}phase $CUR_PHASE: pass, $PHASE_PASSES assertions, $((SECONDS-PHASE_T0))s
"
}
step() { CUR_STEP=$*; printf '\n-- %s\n' "$*"; }
ok()   { PASSES=$((PASSES+1)); PHASE_PASSES=$((PHASE_PASSES+1)); printf 'PASS  %s\n' "$*"; }
fail() {
  FAIL_MSG=$*
  printf '\nFAIL  phase %s, step "%s"\n      %s\n' "$CUR_PHASE" "$CUR_STEP" "$FAIL_MSG"
  dump_state
  exit 1
}
# run_phase PHASE : run it unless it already ran in this pass
run_phase() { case "$DONE " in *" $1 "*) ;; *) "phase_$1";; esac; }
# require PHASE : a phase another phase depends on
require() {
  case "$DONE " in *" $1 "*) return;; esac
  local p=$CUR_PHASE s=$CUR_STEP n=$PHASE_PASSES t=$PHASE_T0
  note "phase $1 is needed first, running it"
  "phase_$1"
  CUR_PHASE=$p; CUR_STEP=$s; PHASE_PASSES=$n; PHASE_T0=$t
}

dump_state() {
  printf '\n--- state at the failure (for diagnosis) ---\n'
  local n
  for n in node-a node-b node-c; do
    printf '[%s] peers: ' "$n"
    api "$n" GET /peers 2>/dev/null | J -c '[.peers[]|{node_name,mesh_ip,reachable,scope}]' 2>/dev/null || echo '(sidecar not answering)'
    printf '[%s] routes on potluck0: ' "$n"; ex "$n" 'ip -4 route show dev potluck0 | awk "{print \$1}" | tr "\n" " "' 2>/dev/null; echo
    printf '[%s] last log lines:\n' "$n"
    ex "$n" 'mesh-ctl log-since-last-start 2>/dev/null | grep -v "\[wg\]" | grep -v level=DEBUG | tail -8' 2>/dev/null | trim | cut -c1-240
  done
}

# ---------------------------------------------------------------- assertions
expect_eq()      { [ "$2" = "$3" ] && ok "$1" || fail "$1: expected '$2', got '$3'"; }
expect_match()   { printf '%s' "$3" | grep -Eq -- "$2" && ok "$1" || fail "$1: expected output matching /$2/, got '$3'"; }
expect_ge()      { [ "$3" -ge "$2" ] && ok "$1 ($3)" || fail "$1: expected at least $2, got $3"; }
expect_nomatch() { printf '%s' "$3" | grep -Eq -- "$2" && fail "$1: output must not match /$2/, got '$3'" || ok "$1"; }
# expect_true DESC cmd... : the command must exit 0
expect_true()    { local d=$1; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else fail "$d: '$*' failed"; fi; }
expect_false()   { local d=$1; shift; if "$@" >/dev/null 2>&1; then fail "$d: '$*' succeeded but must not"; else ok "$d"; fi; }
# wait_for SECONDS DESC cmd... : poll every 2s until the command exits 0
wait_for() {
  local t=$1 d=$2 start=$SECONDS; shift 2
  while ! "$@" >/dev/null 2>&1; do
    [ $((SECONDS-start)) -ge "$t" ] && fail "timed out after ${t}s waiting for: $d"
    sleep 2
  done
  ok "$d (after $((SECONDS-start))s)"
}

# stable_for HOLD TIMEOUT DESC cmd... : pass once the command has exited 0 on every poll for HOLD seconds in a row
stable_for() {
  local hold=$1 t=$2 d=$3 start=$SECONDS since=-1; shift 3
  while :; do
    if "$@" >/dev/null 2>&1; then
      [ "$since" -lt 0 ] && since=$SECONDS
      if [ $((SECONDS-since)) -ge "$hold" ]; then ok "$d (held ${hold}s, after $((SECONDS-start))s)"; return 0; fi
    else
      since=-1
    fi
    [ $((SECONDS-start)) -ge "$t" ] && fail "timed out after ${t}s waiting for: $d (it never held for ${hold}s in a row)"
    sleep 2
  done
}

# ---------------------------------------------------------------- queries
ipof() { case $1 in node-a) echo "$IPA";; node-b) echo "$IPB";; node-c) echo "$IPC";; esac; }
meship()         { api "$1" GET /status | J -r '.mesh_ip // empty'; }
sidecar_pid()    { ex "$1" 'cat /run/mesh.pid' | tr -d '\r'; }
joined()         { api "$1" GET /status | J -e '.mesh_ip!=null and .mesh_ip!="" and .coordinator_connection=="connected"' >/dev/null; }
peer_listed()    { api "$1" GET /peers | J -e --arg n "$2" '.peers[]|select(.node_name==$n)' >/dev/null; }
peer_absent()    { ! peer_listed "$1" "$2"; }
peer_reachable() { api "$1" GET /peers | J -e --arg n "$2" '.peers[]|select(.node_name==$n and .reachable==true)' >/dev/null; }
# peer_names NODE : sorted names /peers lists, space separated
peer_names()     { api "$1" GET /peers | J -r '[.peers[].node_name]|sort|join(" ")'; }
# tunnel_routes NODE : sorted host routes on the mesh interface, space separated
tunnel_routes()  { ex "$1" "ip -4 route show dev potluck0 | awk '{print \$1}' | sort | tr '\n' ' ' | sed 's/ *\$//'"; }
# log_count NODE REGEX : matching lines in the current sidecar run's log (no single quotes in REGEX)
log_count()      { ex "$1" "mesh-ctl log-since-last-start | grep -v '\\[wg\\]' | grep -cE '$2' || true" | tr -d '\r'; }
# log_since_last NODE ANCHOR_REGEX : the log lines from the last line matching the anchor to the end
log_since_last() { ex "$1" "mesh-ctl log-since-last-start | grep -v '\\[wg\\]' | awk '/$2/{buf=\"\"} {buf=buf \$0 \"\\n\"} END{printf \"%s\", buf}'" | trim | cut -c1-260; }
log_lines()      { ex "$1" "mesh-ctl log-since-last-start | grep -v '\\[wg\\]' | grep -v level=DEBUG | grep -E '$2' || true" | trim | cut -c1-260; }
# polled forms, for wait_for (a $(...) argument would be evaluated only once)
log_gt()         { [ "$(log_count "$1" "$2")" -gt "$3" ]; }
names_are()      { [ "$(peer_names "$1")" = "$2" ]; }
node_id_of()     { psql -t -A -c "select id from mesh_nodes where node_name='$1'" | tr -d '\r'; }

# restart_sidecar NODE [ENV=value] : stop and start the sidecar process, wait until it has joined
restart_sidecar() {
  local n=$1 e=${2:-}
  ex "$n" "$e mesh-ctl restart" >/dev/null
  wait_for 60 "$n sidecar restarted and joined" joined "$n"
  wait_for 30 "$n applied its welcome" welcome_applied "$n"
}
# the welcome arrived and its roster was applied (an empty roster logs no "peers configured" line)
welcome_applied() {
  [ "$(log_count "$1" 'mesh welcome received')" -ge 1 ] || return 1
  [ "$(log_count "$1" 'mesh welcome received.*roster_size=0')" -ge 1 ] && return 0
  [ "$(log_count "$1" 'tunnel: peers configured')" -ge 1 ]
}

# ---------------------------------------------------------------- data path
# srv NODE IP [PORT] : (re)start a test HTTP server bound to exactly IP:PORT. Kills the old one by pid.
srv() {
  local n=$1 ip=$2 port=${3:-8080}
  ex "$n" "[ -f /run/srv-$port.pid ] && kill \$(cat /run/srv-$port.pid) 2>/dev/null; nohup python3 /opt/meshlive/tcpserve.py $ip $port >/tmp/srv-$port.log 2>&1 & echo \$! > /run/srv-$port.pid"
  wait_for 10 "$n serves $ip:$port" ex "$n" "ss -ltn | grep -q '$ip:$port '"
}
srv_stop() { ex "$1" "[ -f /run/srv-$2.pid ] && kill \$(cat /run/srv-$2.pid) 2>/dev/null; rm -f /run/srv-$2.pid; true" >/dev/null; }
# tcp_get FROMNODE TOIP [PORT] [TIMEOUT] [CURL-EXTRA] : GET from FROMNODE's own mesh address; prints the body or FAILED
tcp_get() {
  local from=$1 to=$2 port=${3:-8080} t=${4:-8} extra=${5:-} fip
  fip=$(ipof "$from")
  ex "$from" "curl -s -m $t --interface $fip $extra http://$to:$port/ || echo FAILED"
}
# assert_tcp FROMNODE TONODE : a real TCP exchange over the tunnel. The reply must name the server and
# show the client address the server saw, which is FROMNODE's mesh address when the tunnel carried it.
# TCP_RETRY=N (seconds) lets it retry for that long; phase W sets it, see there.
assert_tcp() {
  local out want t0=$SECONDS desc="TCP $1 -> $2 over the tunnel"
  want="served-by=$2 bound=$(ipof "$2") client=$(ipof "$1")"
  out=$(tcp_get "$1" "$(ipof "$2")")
  while [ "$out" != "$want" ] && [ $((SECONDS-t0)) -lt "${TCP_RETRY:-0}" ]; do sleep 2; out=$(tcp_get "$1" "$(ipof "$2")"); done
  [ $((SECONDS-t0)) -gt 3 ] && desc="$desc (took $((SECONDS-t0))s)"
  expect_eq "$desc" "$want" "$out"
}
assert_tcp_blocked() {  # FROMNODE TOIP DESC [PORT] [TIMEOUT] [CURL-EXTRA]
  local out; out=$(tcp_get "$1" "$2" "${4:-8080}" "${5:-5}" "${6:-}")
  expect_eq "$3" FAILED "$out"
}
assert_scope_household() {  # NODE : every listed peer carries "scope":"household" (and there is at least one)
  local total tagged
  total=$(api "$1" GET /peers | J -r '.peers|length')
  tagged=$(api "$1" GET /peers | J -r '[.peers[]|select(.scope=="household")]|length')
  [ "$total" -ge 1 ] || fail "$1 lists no peers, cannot check the scope tag"
  expect_eq "$1: all $total /peers entries carry \"scope\":\"household\"" "$total" "$tagged"
}
# assert_routes NODE 'expected host routes' : the routes on the mesh interface are exactly these
assert_routes() { expect_eq "$1: routes on potluck0 are exactly { ${2:-nothing} }" "$2" "$(tunnel_routes "$1")"; }
# assert_consistent NODE : /peers lists exactly the hosts that have a route on the mesh interface
assert_consistent() {
  local p r
  p=$(api "$1" GET /peers | J -r '[.peers[].mesh_ip]|sort|join(" ")')
  r=$(tunnel_routes "$1")
  expect_eq "$1: /peers addresses { ${p:-none} } match the routes on potluck0" "$p" "$r"
}

# pair NODE NAME : mint a pairing token at the coordinator and enrol that node
pair() {
  local t r
  t=$(coord POST /mesh/pairing-tokens | J -r '.token // empty')
  [ -n "$t" ] || fail "the coordinator gave no pairing token"
  r=$(api "$1" POST /pair "{\"pairing_token\":\"$t\",\"node_name\":\"$2\"}")
  printf '%s' "$r" | J -e '.node_id and .mesh_ip' >/dev/null || fail "pairing $2 failed: $(printf '%s' "$r" | trim | cut -c1-200)"
  ok "paired $2 as $(printf '%s' "$r" | J -r .mesh_ip)"
}

# show NODE : peers and routes, for the transcript
show() {
  printf '[%s] peers: ' "$1"; api "$1" GET /peers | J -c '[.peers[]|{node_name,mesh_ip,reachable,mode:.reachability_mode,scope}]' | trim
  printf '[%s] routes on potluck0: %s\n' "$1" "$(tunnel_routes "$1")"
}
