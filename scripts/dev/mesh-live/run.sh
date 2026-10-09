#!/usr/bin/env bash
# Mesh live check: real potluck-mesh sidecars with real WireGuard devices, in
# containers, against a coordinator and Postgres built from this working tree.
# See README.md. Run it with `make mesh-live`. It is not part of `make test`
# and must not run in CI.
#
#   run.sh [--help]
# Environment:
#   PHASES            phases to run, default "up A B E1 C W S"
#   WITH_E2=1         add E2 (forced rejoin, about 1 to 3 minutes)
#   WITH_R=1          add R (needs the two-minute roster refresh, about 4 minutes)
#   WITH_D=1          add D (own address, duplicates, bad keys, via a stand-in coordinator)
#   WITH_PROTO=1      add the throwaway port-filter prototype (a second stack, phase P)
#   BEFORE_REF=<ref>  also run A and C against the sidecar built from that older commit
#   BEFORE_EXPECT     what the older binary should do with a bad address: accepts (default) or refuses
#   MESH_LIVE_PRUNE_BASE=1  on exit also remove base images this run had to pull
# Exit status: 0 all assertions held, 1 an assertion failed, 2 cannot run here, 130 interrupted.

H="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$H/../../.." && pwd)"

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; fi

# Outer: run this script again with all output copied to the transcript, and
# pass its exit status on. tee -i keeps the transcript alive through Ctrl-C so
# the teardown output is not lost; the no-op trap keeps this shell waiting.
if [ -z "${MESH_LIVE_INNER:-}" ]; then
  mkdir -p "$H/out"
  OUT="$H/out/mesh-live-$(date +%Y%m%d-%H%M%S).log"
  export MESH_LIVE_INNER=1 MESH_LIVE_OUT="$OUT"
  trap ':' INT TERM
  bash "$H/run.sh" "$@" 2>&1 | tee -i "$OUT"
  rc=${PIPESTATUS[0]}
  echo "transcript: $OUT"
  exit "$rc"
fi

set -u
# shellcheck source=lib.sh
. "$H/lib.sh"
export REPO_ROOT PROJECT
VARIANT=after; export VARIANT
INTERRUPTED=; FAIL_MSG=; SUMMARY=; BEFORE_DIR=; TORN_DOWN=; ALL_DONE=
T_START=$SECONDS
PHASES=${PHASES:-"up A B E1${WITH_E2:+ E2} C W${WITH_R:+ R} S${WITH_D:+ D}"}
BEFORE_EXPECT=${BEFORE_EXPECT:-accepts}
BASE_IMAGES="golang:1.22-bookworm python:3.11-slim debian:12-slim postgres:16-alpine"
PRE_IMAGES=
for i in $BASE_IMAGES; do docker image inspect "$i" >/dev/null 2>&1 && PRE_IMAGES="$PRE_IMAGES $i"; done

# ------------------------------------------------------------------ teardown
teardown() {
  [ -n "$TORN_DOWN" ] && return; TORN_DOWN=1
  printf '\n-- teardown\n'
  dc down -v --remove-orphans 2>&1 | tail -n 4
  docker image rm meshlive-sidecar:after meshlive-sidecar:before meshlive-sidecar:proto meshlive-coordinator:local 2>&1 | grep -v "No such image"
  [ -n "$BEFORE_DIR" ] && rm -rf "$BEFORE_DIR"
  if [ -n "${MESH_LIVE_PRUNE_BASE:-}" ]; then
    for i in $BASE_IMAGES; do case "$PRE_IMAGES " in *" $i "*) ;; *) docker image rm "$i" 2>&1 | grep -v "No such image";; esac; done
  fi
  local left
  left="$(docker ps -a -q --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ') containers, $(docker network ls -q --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ') networks, $(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ') volumes"
  echo "left behind by project $PROJECT: $left"
}
on_exit() {
  local rc=$?
  trap '' INT TERM; trap - EXIT
  [ -n "$INTERRUPTED" ] && printf '\nmesh-live: interrupted\n'
  teardown
  printf '\n%s' "$SUMMARY"
  if [ "$rc" -eq 0 ]; then
    printf 'mesh-live: PASSED, %s assertions, %ss, phases:%s\n' "$PASSES" "$((SECONDS-T_START))" "$ALL_DONE"
  elif [ -n "$INTERRUPTED" ]; then
    printf 'mesh-live: INTERRUPTED after %ss (torn down)\n' "$((SECONDS-T_START))"
  elif [ -n "$FAIL_MSG" ]; then
    printf 'mesh-live: FAILED in phase %s, step "%s": %s\n' "$CUR_PHASE" "$CUR_STEP" "$FAIL_MSG"
  else
    printf 'mesh-live: ERROR (exit %s) in phase %s, step "%s"\n' "$rc" "$CUR_PHASE" "$CUR_STEP"
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'INTERRUPTED=1; exit 130' INT TERM

# ------------------------------------------------------------------ builds
build_sidecar() {  # VARIANT CONTEXT_DIR COMMIT [PROTO]
  note "building meshlive-sidecar:$1 from $2"
  docker buildx build --load -f "$H/Dockerfile.sidecar" --build-context harness="$H" \
    --build-arg COMMIT="$3" --build-arg PROTO="${4:-}" -t "meshlive-sidecar:$1" "$2" \
    >"$H/out/build-$1.log" 2>&1 || { tail -n 30 "$H/out/build-$1.log"; fail "the $1 sidecar image did not build (log: out/build-$1.log)"; }
}
build_variant() {
  CUR_PHASE=build; CUR_STEP="build images"
  local sha; sha=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
  [ -n "$(git -C "$REPO_ROOT" status --porcelain -- apps/desktop/go/potluck-mesh 2>/dev/null)" ] && sha="$sha+uncommitted"
  case $1 in
    after) build_sidecar after "$REPO_ROOT/apps/desktop/go/potluck-mesh" "$sha" ;;
    proto) build_sidecar proto "$REPO_ROOT/apps/desktop/go/potluck-mesh" "$sha+portfilter-prototype" portfilter ;;
    before)
      git -C "$REPO_ROOT" rev-parse --verify "${BEFORE_REF}^{commit}" >/dev/null 2>&1 || fail "BEFORE_REF=$BEFORE_REF is not a commit in this repository"
      BEFORE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/meshlive-before.XXXXXX")
      git -C "$REPO_ROOT" archive "$BEFORE_REF" apps/desktop/go/potluck-mesh | tar -x -C "$BEFORE_DIR"
      build_sidecar before "$BEFORE_DIR/apps/desktop/go/potluck-mesh" "$(git -C "$REPO_ROOT" rev-parse --short "$BEFORE_REF")" ;;
  esac
  if ! docker image inspect meshlive-coordinator:local >/dev/null 2>&1; then
    note "building meshlive-coordinator:local from services/coordinator"
    dc build coordinator-migrate >"$H/out/build-coordinator.log" 2>&1 || { tail -n 30 "$H/out/build-coordinator.log"; fail "the coordinator image did not build (log: out/build-coordinator.log)"; }
  fi
  printf 'images: sidecar %s sha256:%s, built from %s\n' "$1" "$(docker run --rm --entrypoint cat "meshlive-sidecar:$1" /etc/potluck-mesh.sha)" \
    "$(docker inspect -f '{{index .Config.Labels "meshlive.commit"}}' "meshlive-sidecar:$1")"
}

# ------------------------------------------------------------------ up
phase_up() {
  phase up "Bring up the stack (sidecar image: $VARIANT)"
  step "containers can get /dev/net/tun and NET_ADMIN"
  docker run --rm --cap-add NET_ADMIN --device /dev/net/tun --entrypoint test "meshlive-sidecar:$VARIANT" -c /dev/net/tun \
    || fail "Docker cannot give a container /dev/net/tun with NET_ADMIN, which the sidecar needs to create its TUN device"
  ok "/dev/net/tun is available to containers"
  step "start postgres, coordinator, stun and three sidecar containers"
  dc down -v --remove-orphans >/dev/null 2>&1
  dc up -d postgres coordinator-migrate coordinator stun node-a node-b node-c >"$H/out/up.log" 2>&1 \
    || { tail -n 30 "$H/out/up.log"; dc logs --tail 30 coordinator-migrate coordinator 2>&1 | tail -n 40; fail "the stack did not come up (log: out/up.log)"; }
  expect_eq "coordinator answers /health" ok "$(coord GET /health | J -r '.status // empty')"
  step "the stack is isolated from the host and the internet"
  expect_eq "docker network is internal" true "$(docker network inspect "${PROJECT}_meshnet" -f '{{.Internal}}')"
  expect_nomatch "no container publishes a port to the host" '->' "$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Ports}}' | tr -d '\n')"
  expect_match "node-a cannot reach the internet" ' exit=[1-9][0-9]*$' "$(ex node-a 'curl -s -m 4 -o /dev/null -w "%{http_code}" https://example.com; echo " exit=$?"')"
  expect_eq "node-a has no default route" 0 "$(ex node-a 'ip route | grep -c "^default" || true')"
  phase_end
}

# ------------------------------------------------------------------ A
phase_A() {
  phase A "A household of two machines pairs and carries traffic both ways [$VARIANT]"
  step "start both sidecars and pair them into one household"
  ex node-a 'mesh-ctl start' >/dev/null; ex node-b 'mesh-ctl start' >/dev/null
  expect_match "household created" '^[0-9a-f-]{36}$' "$(coord POST /mesh/households '{"name":"meshlive"}' | J -r '.id // empty')"
  pair node-a node-a; pair node-b node-b
  wait_for 90 "node-a joined" joined node-a; wait_for 90 "node-b joined" joined node-b
  IPA=$(meship node-a); IPB=$(meship node-b)
  expect_match "node-a mesh address is in 100.64.0.0/10" '^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+$' "$IPA"
  [ "$IPA" != "$IPB" ] && ok "node-b has its own address ($IPB)" || fail "both nodes got $IPA"
  step "each machine sees the other with a fresh handshake"
  wait_for 60 "node-a sees node-b reachable" peer_reachable node-a node-b
  wait_for 60 "node-b sees node-a reachable" peer_reachable node-b node-a
  show node-a; show node-b
  if [ "$VARIANT" != before ]; then
    step "every /peers entry carries the household scope tag"
    assert_scope_household node-a; assert_scope_household node-b
  fi
  step "data flows both ways (a TCP exchange, not a handshake)"
  srv node-a "$IPA"; srv node-b "$IPB"
  assert_tcp node-a node-b; assert_tcp node-b node-a
  step "routes and firewall"
  assert_routes node-a "$IPB"; assert_routes node-b "$IPA"
  assert_consistent node-a; assert_consistent node-b
  expect_match "node-a opens the mesh interface and the WireGuard port" '-i potluck0 -j ACCEPT[[:space:]]*.*--dport 51820 -j ACCEPT|--dport 51820 -j ACCEPT[[:space:]]*.*-i potluck0 -j ACCEPT' "$(ex node-a 'iptables -S INPUT | tr "\n" " "')"
  expect_eq "node-a dropped no roster entry" 0 "$(log_count node-a 'roster: dropping peer')"
  expect_eq "node-b dropped no roster entry" 0 "$(log_count node-b 'roster: dropping peer')"
  phase_end
}

# ------------------------------------------------------------------ B
phase_B() {
  phase B "A third machine joins and is removed while the others keep running [$VARIANT]"
  require A
  step "node-c joins; the others learn of it without a restart"
  ex node-c 'mesh-ctl start' >/dev/null; sleep 2
  local pa pb; pa=$(sidecar_pid node-a); pb=$(sidecar_pid node-b)
  pair node-c node-c
  wait_for 60 "node-a lists node-c" peer_listed node-a node-c
  wait_for 60 "node-b lists node-c" peer_listed node-b node-c
  wait_for 60 "node-a sees node-c reachable" peer_reachable node-a node-c
  wait_for 60 "node-b sees node-c reachable" peer_reachable node-b node-c
  wait_for 60 "node-c sees node-a reachable" peer_reachable node-c node-a
  wait_for 60 "node-c sees node-b reachable" peer_reachable node-c node-b
  IPC=$(meship node-c); srv node-c "$IPC"
  expect_eq "node-a and node-b were not restarted" "$pa $pb" "$(sidecar_pid node-a) $(sidecar_pid node-b)"
  if [ "$VARIANT" != before ]; then
    step "every /peers entry carries the household scope tag"
    assert_scope_household node-a; assert_scope_household node-b; assert_scope_household node-c
  fi
  step "all six directions carry TCP"
  assert_tcp node-a node-b; assert_tcp node-b node-a; assert_tcp node-a node-c
  assert_tcp node-c node-a; assert_tcp node-b node-c; assert_tcp node-c node-b
  assert_routes node-a "$IPB $IPC"; assert_routes node-b "$IPA $IPC"
  step "the owner removes node-c at the coordinator"
  # The sidecar's own /unpair only wipes local keys and does not tell the coordinator.
  ex node-c 'curl -s --unix-socket /run/potluck-mesh.sock -X POST http://localhost/unpair -o /dev/null -w "unpair http=%{http_code}\n"'
  local cid code; cid=$(node_id_of node-c)
  code=$(dc exec -T node-a curl -s -m 20 -X DELETE -o /dev/null -w '%{http_code}' "http://coordinator:8400/mesh/households/current/machines/$cid" </dev/null)
  expect_match "coordinator accepts the removal" '^20[0-9]$' "$code"
  wait_for 30 "node-a drops node-c" peer_absent node-a node-c
  wait_for 30 "node-b drops node-c" peer_absent node-b node-c
  assert_routes node-a "$IPB"; assert_routes node-b "$IPA"
  expect_nomatch "node-a has no route to node-c's address through the tunnel" ' dev potluck0 ' "$(ex node-a "ip route get $IPC 2>&1")"
  assert_tcp_blocked node-a "$IPC" "node-a cannot reach the removed node-c" 8080 4
  assert_tcp node-a node-b; assert_tcp node-b node-a
  phase_end
}

# ------------------------------------------------------------------ E1, E2
phase_E1() {
  phase E1 "A sidecar process restarts and the pair recovers [$VARIANT]"
  require A
  step "restart node-b's sidecar"
  local pa; pa=$(sidecar_pid node-a)
  restart_sidecar node-b
  wait_for 60 "node-a sees node-b reachable" peer_reachable node-a node-b
  wait_for 60 "node-b sees node-a reachable" peer_reachable node-b node-a
  srv node-b "$IPB"
  assert_tcp node-a node-b; assert_tcp node-b node-a
  expect_eq "node-a was not restarted" "$pa" "$(sidecar_pid node-a)"
  assert_routes node-a "$IPB"; assert_routes node-b "$IPA"
  phase_end
}
phase_E2() {
  phase E2 "A fast reboot: the coordinator sees the new connection before it notices the old one died [$VARIANT]"
  require A
  step "drop the old socket's packets, kill -9 node-b's sidecar, start it again"
  local port; port=$(ex node-b "ss -tn state established '( dport = :8400 )' | awk 'NR>1{split(\$3,a,\":\"); print a[2]}' | head -1")
  expect_match "found node-b's connection to the coordinator" '^[0-9]+$' "$port"
  ex node-b "iptables -I OUTPUT 1 -p tcp --sport $port -j DROP; mesh-ctl kill9; mesh-ctl start" >/dev/null
  wait_for 90 "node-b joined again" joined node-b
  note "the coordinator's cleanup of the old connection can take node-b out of node-a's roster a while after it rejoined; node-a's two-minute refresh brings it back"
  stable_for 60 300 "node-a sees node-b reachable, continuously" peer_reachable node-a node-b
  wait_for 60 "node-b sees node-a reachable" peer_reachable node-b node-a
  srv node-b "$IPB"
  assert_tcp node-a node-b; assert_tcp node-b node-a
  ex node-b "iptables -D OUTPUT -p tcp --sport $port -j DROP" >/dev/null
  phase_end
}

# ------------------------------------------------------------------ C
# ensure_c : node-c is enrolled at the coordinator at its own address, reachable from node-a, with a server on it
ensure_c() {
  if peer_reachable node-a node-c >/dev/null 2>&1 && [ -n "$IPC" ]; then return; fi
  note "setting node-c up (a fresh key, because an earlier unpair wipes the old one)"
  ex node-c 'mesh-ctl restart' >/dev/null; sleep 2
  pair node-c node-c
  wait_for 60 "node-a lists node-c" peer_listed node-a node-c
  wait_for 60 "node-a sees node-c reachable" peer_reachable node-a node-c
  IPC=$(meship node-c)
}
# bad_row_setup VALUE : stand-in LAN on node-a, put VALUE in node-c's row, restart node-c then node-a
bad_row_setup() {
  ex node-a "ip addr show dev eth0 | grep -q 192.168.50.7 || ip addr add 192.168.50.7/24 dev eth0" >/dev/null
  srv node-c "$IPC"; srv node-a "$IPA"; srv node-b "$IPB"
  set_ip node-c "$1" >/dev/null
  ex node-c "mesh-ctl restart" >/dev/null
  wait_for 60 "node-c joined with the new row" joined node-c
  srv node-c 192.168.50.1
  restart_sidecar node-a
}
phase_C() {
  phase C "A bad roster address is refused, only that entry, and nothing is routed to it [$VARIANT]"
  require A; ensure_c
  local cid; cid=$(node_id_of node-c)
  step "baseline traffic before any bad row"
  srv node-a "$IPA"; srv node-b "$IPB"; srv node-c "$IPC"
  assert_tcp node-a node-b; assert_tcp node-a node-c
  step "node-c's row says 192.168.50.1, a LAN gateway address (node-a gets 192.168.50.7/24 so it is on-link); node-c and then node-a restart"
  bad_row_setup 192.168.50.1
  expect_ge "node-a logged why it dropped node-c (outside the mesh range 100.64.0.0/10)" 1 "$(log_count node-a "roster: dropping peer.*node_id=$cid.*outside the mesh range 100.64.0.0/10")"
  expect_eq "node-a lists only node-b" "node-b" "$(peer_names node-a)"
  assert_routes node-a "$IPB"
  expect_eq "no 'peer added' line for 192.168.50.1 on node-a" 0 "$(log_count node-a 'tunnel: peer added.*192\.168\.50\.1')"
  expect_match "node-a reaches 192.168.50.1 on eth0, not through the tunnel" ' dev eth0 ' "$(ex node-a 'ip route get 192.168.50.1')"
  expect_nomatch "nothing answers 192.168.50.1 through the tunnel" 'served-by' "$(ex node-a 'curl -s -m 4 --connect-timeout 3 http://192.168.50.1:8080/ || echo no-answer')"
  step "node-b was not restarted; it saw the same change on the live signal channel"
  expect_eq "node-b lists only node-a" "node-a" "$(peer_names node-b)"
  assert_routes node-b "$IPA"
  step "the entry beside it is unaffected"
  wait_for 60 "node-a sees node-b reachable" peer_reachable node-a node-b
  assert_tcp node-a node-b; assert_tcp node-b node-a

  step "other bad values through the welcome path (set the row, restart node-a, look)"
  local entry v want
  for entry in '10.9.9.9|outside the mesh range' '127.0.0.1|loopback address' '169.254.169.254|link-local address' \
              '0.0.0.0|unspecified address' '100.127.255.255|broadcast address' '100.64.0.5/24|not a plain IPv4' \
              '::ffff:100.64.0.1|not a plain IPv4' 'fd00::3|not a plain IPv4'; do
    v=${entry%%|*}; want=${entry#*|}
    printf '\n----- node-c mesh_ip = %s\n' "$v"
    set_ip node-c "$v" >/dev/null
    restart_sidecar node-a
    expect_ge "node-a logged why it dropped '$v' ($want)" 1 "$(log_count node-a "roster: dropping peer.*node_id=$cid.*$want")"
    expect_eq "node-a lists only node-b" "node-b" "$(peer_names node-a)"
    assert_routes node-a "$IPB"
    wait_for 60 "node-a sees node-b reachable" peer_reachable node-a node-b
    assert_tcp node-a node-b
  done
  step "restore: node-c back at its own address"
  set_ip node-c "$IPC" >/dev/null
  restart_sidecar node-c; restart_sidecar node-a
  wait_for 90 "node-a sees node-c reachable" peer_reachable node-a node-c
  wait_for 60 "node-a sees node-b reachable" peer_reachable node-a node-b
  srv node-c "$IPC"; assert_tcp node-a node-c
  ex node-a "ip addr del 192.168.50.7/24 dev eth0" >/dev/null
  phase_end
}
# C against the older binary: the same setup, then record what it did. A negative control for phase C.
phase_Cbefore() {
  phase Cbefore "What the older binary does with a bad roster address (BEFORE_REF=$BEFORE_REF, expected: $BEFORE_EXPECT)"
  require A; ensure_c
  step "node-c's row says 192.168.50.1; node-c and then node-a restart"
  bad_row_setup 192.168.50.1
  show node-a
  local listed routed reached
  listed=$(api node-a GET /peers | J -r '[.peers[]|select(.mesh_ip=="192.168.50.1")]|length')
  routed=$(ex node-a 'ip -4 route show dev potluck0 | grep -c "^192.168.50.1 " || true' | tr -d '\r')
  if [ "$listed" -ge 1 ]; then wait_for 40 "the older binary completes a handshake with 192.168.50.1" peer_reachable node-a node-c; fi
  reached=$(ex node-a 'curl -s -m 4 --connect-timeout 3 http://192.168.50.1:8080/ || echo no-answer')
  note "observed: listed=$listed routed=$routed answer='$reached'"
  case $BEFORE_EXPECT in
    accepts) [ "$listed" -ge 1 ] && [ "$routed" -ge 1 ] && ok "the older binary lists and routes 192.168.50.1 (the problem phase C guards against is visible)" \
               || fail "BEFORE_REF=$BEFORE_REF refused the bad address (listed=$listed routed=$routed). Pick a commit from before the roster check, or set BEFORE_EXPECT=refuses" ;;
    refuses) [ "$listed" -eq 0 ] && [ "$routed" -eq 0 ] && ok "the older binary also refuses 192.168.50.1" \
               || fail "BEFORE_REF=$BEFORE_REF accepted the bad address (listed=$listed routed=$routed) but BEFORE_EXPECT=refuses" ;;
    *) fail "BEFORE_EXPECT must be accepts or refuses, got '$BEFORE_EXPECT'" ;;
  esac
  if [ "$BEFORE_EXPECT" = accepts ]; then
    expect_match "the older binary sends 192.168.50.1 traffic into the tunnel and node-c answers it" 'served-by=node-c bound=192.168.50.1' "$reached"
  fi
  ex node-a "ip addr del 192.168.50.7/24 dev eth0" >/dev/null
  phase_end
}

# ------------------------------------------------------------------ W
phase_W() {
  phase W "Refuse, then correct, while every sidecar keeps running [$VARIANT]"
  require A; ensure_c
  step "node-c announces its endpoint every 15s so the coordinator broadcasts its row to node-a and node-b"
  set_ip node-c "$IPC" >/dev/null
  ex node-c 'STUN_REFRESH=15s mesh-ctl restart' >/dev/null
  wait_for 60 "node-c joined" joined node-c
  srv node-c "$IPC"; srv node-a "$IPA"; srv node-b "$IPB"
  wait_for 60 "node-a sees node-c reachable" peer_reachable node-a node-c
  wait_for 60 "node-b sees node-c reachable" peer_reachable node-b node-c
  local pa pb pc cid; pa=$(sidecar_pid node-a); pb=$(sidecar_pid node-b); pc=$(sidecar_pid node-c); cid=$(node_id_of node-c)
  note "sidecar pids from here to the end of this phase: node-a=$pa node-b=$pb node-c=$pc"
  # Every 15s node-c's STUN refresh makes the coordinator broadcast its endpoint. The logs show a peer applying
  # that endpoint and a punch moving it back to the LAN address a few milliseconds later. A TCP try that lands
  # in or just after that window has taken one WireGuard handshake retry (about 5s) or failed once.
  # The checks here retry for 30s; the transcript says when one needed more than 3s.
  TCP_RETRY=30
  assert_consistent node-a; assert_consistent node-b
  assert_tcp node-a node-c; assert_tcp node-c node-a
  local round=0 bad want t0 adds0
  adds0=$(log_count node-a "tunnel: peer added.*allowed_ips=\[$IPC/32\]")
  for bad in '10.9.9.9|outside the mesh range' '100.64.0.5/24|not a plain IPv4'; do
    round=$((round+1)); want=${bad#*|}; bad=${bad%%|*}
    step "round $round, valid to invalid ($bad): nothing restarts"
    t0=$SECONDS
    set_ip node-c "$bad" >/dev/null
    wait_for 60 "node-a drops node-c" peer_absent node-a node-c
    wait_for 60 "node-b drops node-c" peer_absent node-b node-c
    note "elapsed since the row change: $((SECONDS-t0))s"
    expect_ge "node-a logged why it dropped node-c ($want)" 1 "$(log_count node-a "roster: dropping peer.*node_id=$cid.*$want")"
    assert_routes node-a "$IPB"; assert_routes node-b "$IPA"
    assert_consistent node-a; assert_consistent node-b
    expect_nomatch "node-a has no route to node-c's address" ' dev potluck0 ' "$(ex node-a "ip route get $IPC 2>&1")"
    assert_tcp_blocked node-a "$IPC" "node-a cannot reach node-c while its row is bad" 8080 4
    step "round $round, corrected ($IPC): nothing restarts"
    t0=$SECONDS
    set_ip node-c "$IPC" >/dev/null
    wait_for 60 "node-a lists node-c again" peer_listed node-a node-c
    wait_for 60 "node-b lists node-c again" peer_listed node-b node-c
    note "elapsed since the row change: $((SECONDS-t0))s"
    wait_for 40 "node-a sees node-c reachable" peer_reachable node-a node-c
    wait_for 40 "node-b sees node-c reachable" peer_reachable node-b node-c
    assert_routes node-a "$IPB $IPC"; assert_routes node-b "$IPA $IPC"
    assert_consistent node-a; assert_consistent node-b
    expect_ge "node-a set node-c up as a new peer again after correction $round (peer-added lines for it since the phase began)" "$round" "$(( $(log_count node-a "tunnel: peer added.*allowed_ips=\[$IPC/32\]") - adds0 ))"
    assert_tcp node-a node-c; assert_tcp node-c node-a; assert_tcp node-b node-c; assert_tcp node-c node-b
  done
  TCP_RETRY=0
  step "no sidecar was restarted"
  expect_eq "sidecar pids unchanged" "$pa $pb $pc" "$(sidecar_pid node-a) $(sidecar_pid node-b) $(sidecar_pid node-c)"
  for n in node-a node-b node-c; do note "$n: roster refresh repairs in this run: $(log_count $n 'roster refresh: reconciling')"; done
  phase_end
}

# ------------------------------------------------------------------ R (flag)
phase_R() {
  phase R "The two-minute roster re-read repairs a missed update [$VARIANT]"
  require A; ensure_c
  step "node-c announces nothing during this test (20 minute STUN refresh), so only the re-read can bring it back"
  set_ip node-c "$IPC" >/dev/null
  ex node-c 'STUN_REFRESH=20m mesh-ctl restart' >/dev/null
  wait_for 60 "node-c joined" joined node-c
  srv node-c "$IPC"; srv node-a "$IPA"
  wait_for 60 "node-a sees node-c reachable" peer_reachable node-a node-c
  step "R1: the row is invalid when node-a restarts and fixed right after"
  set_ip node-c 10.9.9.9 >/dev/null
  restart_sidecar node-a
  expect_eq "node-a lists only node-b after its restart" node-b "$(peer_names node-a)"
  set_ip node-c "$IPC" >/dev/null
  note "waiting for node-a's refresh (every 2 minutes after its start)"
  wait_for 170 "node-a lists node-c again (from the refresh)" peer_listed node-a node-c
  expect_eq "the refresh added exactly node-c" 1 "$(log_count node-a 'roster refresh: reconciling.*added=1')"
  wait_for 40 "node-a sees node-c reachable" peer_reachable node-a node-c
  assert_routes node-a "$IPB $IPC"
  assert_tcp node-a node-c; assert_tcp node-c node-a
  step "R2: the row turns invalid while node-a runs and nothing else changes (observed, not asserted)"
  set_ip node-c 10.9.9.9 >/dev/null
  note "waiting 135s for one full refresh period"; sleep 135
  note "observed after the refresh: node-a lists '$(peer_names node-a)', routes '$(tunnel_routes node-a)'"
  set_ip node-c "$IPC" >/dev/null
  phase_end
}

# ------------------------------------------------------------------ S
phase_S() {
  phase S "A fresh welcome reaches a running sidecar: the coordinator restarts, no sidecar does [$VARIANT]"
  require A; ensure_c
  step "node-c is listed and reachable, then its row turns invalid"
  set_ip node-c "$IPC" >/dev/null
  wait_for 60 "node-a lists node-c" peer_listed node-a node-c
  wait_for 60 "node-a sees node-c reachable" peer_reachable node-a node-c
  assert_routes node-a "$IPB $IPC"
  local pa pb w0 c0 cid; pa=$(sidecar_pid node-a); pb=$(sidecar_pid node-b); cid=$(node_id_of node-c)
  w0=$(log_count node-a 'mesh welcome received'); c0=$(log_count node-a 'tunnel: peers configured')
  set_ip node-c 10.9.9.9 >/dev/null
  step "restart only the coordinator; every sidecar reconnects and gets a new welcome"
  dc restart coordinator 2>&1 | tail -n 2
  wait_for 120 "node-a applied a second welcome" log_gt node-a 'mesh welcome received' "$w0"
  wait_for 30 "node-a applied it (peers configured)" log_gt node-a 'tunnel: peers configured' "$c0"
  local blk; blk=$(log_since_last node-a 'mesh welcome received' | head -6)
  expect_match "the new welcome itself dropped node-c ($cid)" "roster: dropping peer.*node_id=$cid.*outside the mesh range" "$blk"
  expect_eq "node-a and node-b are the same processes" "$pa $pb" "$(sidecar_pid node-a) $(sidecar_pid node-b)"
  expect_eq "node-a lists only node-b" node-b "$(peer_names node-a)"
  assert_routes node-a "$IPB"
  expect_nomatch "node-a has no route to node-c's address" ' dev potluck0 ' "$(ex node-a "ip route get $IPC 2>&1")"
  wait_for 60 "node-b reconnected and lists only node-a" names_are node-b node-a
  assert_routes node-b "$IPA"
  wait_for 60 "node-a sees node-b reachable" peer_reachable node-a node-b
  assert_tcp node-a node-b; assert_tcp node-b node-a
  step "restore: node-c at its own address again"
  set_ip node-c "$IPC" >/dev/null
  restart_sidecar node-c
  wait_for 90 "node-a lists node-c" peer_listed node-a node-c
  wait_for 60 "node-a sees node-c reachable" peer_reachable node-a node-c
  srv node-c "$IPC"; assert_tcp node-a node-c
  phase_end
}

# ------------------------------------------------------------------ D (flag)
phase_D() {
  phase D "Own address, duplicates and bad keys [$VARIANT]"
  require A; ensure_c
  step "D1: through the real coordinator database, which forbids duplicate addresses"
  expect_match "the database refuses node-c at node-a's address" 'duplicate key' "$(psql -c "update mesh_nodes set mesh_ip='$IPA' where node_name='node-c'" 2>&1)"
  expect_match "the database refuses node-c at node-b's address" 'duplicate key' "$(psql -c "update mesh_nodes set mesh_ip='$IPB' where node_name='node-c'" 2>&1)"
  expect_match "the database refuses a swap that makes two rows one address" 'duplicate key' "$(psql -c "update mesh_nodes set mesh_ip = case node_name when 'node-b' then '$IPC'::inet else '$IPB'::inet end where node_name in ('node-b','node-c')" 2>&1)"
  note "spellings of node-a's own address that Postgres INET keeps as different values"
  local v cid; cid=$(node_id_of node-c)
  for v in "$IPA/24" "::ffff:$IPA"; do
    set_ip node-c "$v" >/dev/null
    restart_sidecar node-a
    expect_eq "node-a refused '$v'" 1 "$(log_count node-a "roster: dropping peer.*node_id=$cid.*not a plain IPv4")"
    expect_eq "node-a lists only node-b" node-b "$(peer_names node-a)"
    assert_routes node-a "$IPB"
  done
  set_ip node-c "$IPC" >/dev/null
  restart_sidecar node-a
  step "D2: a stand-in coordinator (not the real one) sends what the database cannot. node-d is a fourth sidecar on it"
  dc up -d fakecoord node-d >"$H/out/up-fake.log" 2>&1 || fail "fakecoord and node-d did not start (log: out/up-fake.log)"
  ex node-d 'mesh-ctl start' >/dev/null
  wait_for 40 "node-d joined" joined node-d
  wait_for 20 "node-d applied the welcome" welcome_applied node-d
  note "welcome: self=100.64.9.9; roster = good-1, claims-own (our address), dup-A + dup-B (one address), bad-key, short-key, good-2"
  expect_eq "node-d lists exactly the two good entries" "good-1 good-2" "$(peer_names node-d)"
  expect_eq "node-d refused the entry that claims its own address" 1 "$(log_count node-d "roster: dropping peer.*this node.s own address")"
  expect_eq "node-d refused both entries that share an address" 2 "$(log_count node-d "roster: dropping peer.*claimed by more than one node")"
  expect_eq "node-d refused both bad keys" 2 "$(log_count node-d "roster: dropping peer.*not a canonical base64 32-byte key")"
  expect_eq "node-d routes only the two good entries" "100.64.9.10 100.64.9.11" "$(tunnel_routes node-d)"
  local push_n=0
  push() {  # JSON : hand the stand-in one live message
    push_n=$((push_n+1))
    printf '%s' "$1" | dc exec -T fakecoord sh -c "cat > /tmp/push/$(printf '%02d' $push_n).json"
  }
  fk() { dc exec -T fakecoord sh -c "python3 -c 'import json;d=json.load(open(\"/tmp/fake_keys.json\"));$1'" </dev/null; }
  step "P1: a live update adds a newcomer that claims good-2's address; neither may keep it"
  push "{\"type\":\"roster_update\",\"added\":[$(fk 'print(json.dumps(d["new"]["newcomer"]))')],\"updated\":[],\"removed\":[]}"
  wait_for 20 "node-d now lists only good-1" names_are node-d good-1
  assert_routes node-d "100.64.9.10"
  step "P2: a live update makes good-1 claim node-d's own address"
  push "{\"type\":\"roster_update\",\"added\":[],\"updated\":[$(fk 'p=d["peers"][0];p["mesh_ip"]="100.64.9.9";print(json.dumps(p))')],\"removed\":[]}"
  wait_for 20 "node-d lists no peers" names_are node-d ""
  assert_routes node-d ""
  step "P3: a live update adds a valid peer, which still gets in"
  push "{\"type\":\"roster_update\",\"added\":[$(fk 'print(json.dumps(d["new"]["good3"]))')],\"updated\":[],\"removed\":[]}"
  wait_for 20 "node-d lists good-3" names_are node-d good-3
  assert_routes node-d "100.64.9.12"
  step "none of the keys the stand-in handed out appears in node-d's log"
  local keys log k hits=0 n=0
  keys=$(fk 'print("\n".join(d["keys"].values()))')
  log=$(ex node-d 'cat /var/log/mesh/mesh.log')
  for k in $keys; do
    n=$((n+1))
    if printf '%s' "$log" | grep -qF -- "$k" || printf '%s' "$log" | grep -qF -- "${k:0:20}"; then hits=$((hits+1)); fi
  done
  expect_eq "keys checked: $n, found in the log" 0 "$hits"
  dc rm -sf node-d fakecoord >/dev/null 2>&1
  phase_end
}

# ------------------------------------------------------------------ P (flag): port-filter prototype
phase_P() {
  phase P "Port-filter prototype: a circle peer reaches one TCP port and nothing else [$VARIANT]"
  step "node-a's sidecar runs the prototype; node-b is the circle peer (100.64.0.2), node-c a household peer"
  # In this phase "circle peer" is only a setting on node-a: PORTFILTER_PEERS. node-a lets the circle peer
  # reach TCP 8080 on itself and lets itself reach TCP 9090 on the circle peer. Everything else to and from it is dropped.
  ex node-a 'PORTFILTER_PEERS=100.64.0.2 PORTFILTER_LOCAL_PORT=8080 PORTFILTER_REMOTE_PORT=9090 mesh-ctl start' >/dev/null
  ex node-b 'mesh-ctl start' >/dev/null; ex node-c 'mesh-ctl start' >/dev/null
  expect_match "household created" '^[0-9a-f-]{36}$' "$(coord POST /mesh/households '{"name":"meshlive"}' | J -r '.id // empty')"
  pair node-a node-a; pair node-b node-b; pair node-c node-c
  wait_for 60 "node-a joined" joined node-a; wait_for 60 "node-b joined" joined node-b; wait_for 60 "node-c joined" joined node-c
  IPA=$(meship node-a); IPB=$(meship node-b); IPC=$(meship node-c)
  expect_match "node-a started the prototype filter" 'PORT FILTER PROTOTYPE ACTIVE.*peers=1 local_port=8080 remote_port=9090' "$(log_lines node-a 'PORT FILTER')"
  expect_eq "node-b got the address the filter treats as the circle peer" 100.64.0.2 "$IPB"
  wait_for 60 "node-a sees node-b reachable" peer_reachable node-a node-b
  wait_for 60 "node-a sees node-c reachable" peer_reachable node-a node-c
  wait_for 60 "node-b sees node-a reachable" peer_reachable node-b node-a
  wait_for 60 "node-c sees node-a reachable" peer_reachable node-c node-a
  step "inbound: the circle peer to this host"
  srv node-a "$IPA" 8080; srv node-a "$IPA" 8081
  expect_eq "node-b reaches node-a TCP 8080" "served-by=node-a bound=$IPA client=$IPB" "$(tcp_get node-b "$IPA" 8080)"
  assert_tcp_blocked node-b "$IPA" "node-b cannot reach node-a TCP 8081 (a server is listening there)" 8081 5
  assert_tcp_blocked node-b "$IPA" "node-b cannot open 8081 from its source port 9090 (the reply-direction hole)" 8081 5 "--local-port 9090"
  expect_eq "control: node-b can use source port 9090 to reach node-a TCP 8080, so the line above was the filter" "served-by=node-a bound=$IPA client=$IPB" "$(tcp_get node-b "$IPA" 8080 8 "--local-port 9090")"
  expect_eq "node-c, a household peer, reaches node-a TCP 8081" "served-by=node-a bound=$IPA client=$IPC" "$(tcp_get node-c "$IPA" 8081)"
  expect_eq "node-b still reaches node-a TCP 8080 after the blocked attempts" "served-by=node-a bound=$IPA client=$IPB" "$(tcp_get node-b "$IPA" 8080)"
  step "outbound: this host to the circle peer"
  srv node-b "$IPB" 9090; srv node-b "$IPB" 9091; srv node-c "$IPC" 8081
  expect_eq "node-a reaches node-b TCP 9090 (the peer's circle port)" "served-by=node-b bound=$IPB client=$IPA" "$(tcp_get node-a "$IPB" 9090)"
  assert_tcp_blocked node-a "$IPB" "node-a cannot reach node-b TCP 9091" 9091 5
  expect_eq "node-a reaches household node-c on TCP 8081" "served-by=node-c bound=$IPC client=$IPA" "$(tcp_get node-a "$IPC" 8081)"
  step "the filter ran, and a long transfer through it arrives whole"
  expect_true "node-a's sidecar logged dropped packets" log_gt node-a 'portfilter: dropped a packet' 0
  local sz=30000000 filtered open
  filtered=$(ex node-b "curl -s -m 60 --interface $IPB -o /dev/null -w '%{size_download} %{speed_download}' http://$IPA:8080/bytes/$sz")
  expect_match "node-b downloaded all $sz bytes from node-a:8080 through the filter" "^$sz " "$filtered"
  open=$(ex node-c "curl -s -m 60 --interface $IPC -o /dev/null -w '%{size_download} %{speed_download}' http://$IPA:8080/bytes/$sz")
  expect_match "node-c downloaded all $sz bytes from node-a:8080 (no filter for it)" "^$sz " "$open"
  note "speed through the filter (circle peer b): ${filtered#* } bytes/s; unfiltered household peer c: ${open#* } bytes/s. Docker Desktop VM numbers, only a sanity check"
  phase_end
}

# ------------------------------------------------------------------ passes
run_pass() {  # VARIANT PHASE...
  VARIANT=$1; shift; export VARIANT
  DONE=; IPA=; IPB=; IPC=
  build_variant "$VARIANT"
  local p
  for p in "$@"; do
    run_phase "$p"
  done
  ALL_DONE="$ALL_DONE $VARIANT($(echo $DONE | tr ' ' ','))"
  dc down -v --remove-orphans >/dev/null 2>&1
}

docker info >/dev/null 2>&1 || { echo "mesh-live: the Docker daemon is not running" >&2; exit 2; }
docker compose version >/dev/null 2>&1 || { echo "mesh-live: needs 'docker compose' (v2)" >&2; exit 2; }
docker buildx version >/dev/null 2>&1 || { echo "mesh-live: needs 'docker buildx'" >&2; exit 2; }
printf 'mesh-live: start %s, repo %s, phases: %s%s%s\n' "$(date -u +%FT%TZ)" "$REPO_ROOT" "$PHASES" \
  "${BEFORE_REF:+, plus A and C on BEFORE_REF=$BEFORE_REF}" "${WITH_PROTO:+, plus the port-filter prototype}"

# shellcheck disable=SC2086
run_pass after $PHASES
if [ -n "${BEFORE_REF:-}" ]; then run_pass before up A Cbefore; fi
if [ -n "${WITH_PROTO:-}" ]; then run_pass proto up P; fi
exit 0
