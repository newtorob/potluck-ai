#!/usr/bin/env bash
# Phases for WITH_LINKS=1: direct circle links between two households. Sourced
# by run.sh after lib.sh; not meant to be run alone. Written for bash 3.2.
#
# The stack for these phases:
#   household 1   node-a (the requester's machine), node-b      created over HTTP as the dev stub member
#   household 2   node-e (the lender's machine), node-f         rows in the database (see L0)
#   the circle    created by the stub member; member 2 is a row
#   lender engine a stand-in that attests as member 2 (container/lender_engine.py), and a
#                 gate on node-e (container/circlegate.py) that checks the token against /circle-peers
#   DERP relay    services/derp, for the relay policy
# One pass runs the phases under one transport policy, see links_pass in run.sh.

LINK_MODEL=meshlive-link-model; export LINK_MODEL
LINK_TIMES=
LINK_PORT=8347
IPE=; IPF=; CIRCLE=; M2=; NODEA_ID=; NODEE_ID=
LINK=; TOKEN=; LCODE=; LBODY=
LINK_POLICY=-

# ---------------------------------------------------------------- helpers
now_ms()  { perl -MTime::HiRes=time -e 'printf "%d", time*1000'; }
cpeers()  { api "$1" GET /circle-peers; }
cp_count(){ cpeers "$1" | J -r '.peers|length'; }
cp_empty(){ [ "$(cp_count "$1")" = 0 ]; }
cp_has()  { cpeers "$1" | J -e --arg ip "$2" '.peers[]|select(.mesh_ip==$ip)' >/dev/null; }
cp_reachable() { cpeers "$1" | J -e --arg ip "$2" '.peers[]|select(.mesh_ip==$ip and .reachable==true)' >/dev/null; }
cp_mode() { cpeers "$1" | J -r --arg ip "$2" '[.peers[]|select(.mesh_ip==$ip)|.reachability_mode][0] // "none"'; }
cp_expires() { cpeers "$1" | J -r --arg ip "$2" '[.peers[]|select(.mesh_ip==$ip)|(.inbound // .outbound).expires_in_s][0] // -1'; }
sha256_of() { ex stun "printf %s '$1' | sha256sum | cut -d' ' -f1" | tr -d '\r'; }
rlines() { ex "$1" "mesh-ctl log-since-last-start | grep -v '\\[wg\\]' | grep -E '$2' || true" | trim | cut -c1-260; }

# udp_count NODE IP : packets the counting rule for UDP to IP has matched on NODE
udp_count() { ex "$1" "iptables -L OUTPUT -v -n -x | awk '\$0 ~ /$2/ {print \$1; exit}'" | tr -d '\r'; }
routes_are() { [ "$(tunnel_routes "$1")" = "$2" ]; }
cp_mode_is() { [ "$(cp_mode "$1" "$2")" = "$3" ]; }
both_empty() { cp_empty node-a && cp_empty node-e; }
engine_log()     { dc logs --no-log-prefix lender-engine 2>&1; }
engine_accepts() { engine_log | grep -c 'accepted .*bound=True' || true; }
engine_bound()   { engine_log | grep -q 'accepted .*circle_links=True bound=True'; }
engine_newer_than() { [ "$(engine_accepts)" -gt "$1" ]; }
# signal_token_bound NODE_ID : the coordinator's latest mesh_signal_connected line for that node says
# token_bound=True. The coordinator offers circle links only on a /mesh/signal connection that the node's
# own node token opened (a sidecar gets that token from /pair and sends it on every dial), so a sidecar on
# any other connection shows up in the link phases only as a 409 requester_sidecar_unsupported or no_lender.
# Colour codes are stripped first; the pattern also reads a JSON log line.
signal_token_bound() {
  [ -n "$1" ] || return 1
  dc logs --no-log-prefix coordinator 2>&1 | perl -pe 's/\e\[[0-9;]*m//g' | grep -F 'mesh_signal_connected' \
    | grep -F "$1" | tail -n 1 | grep -Eq 'token_bound"?[=:] ?"?[Tt]rue'
}
coordinator_up()   { [ -n "$(dc ps -q --status running coordinator)" ] && coord GET /health | J -e '.status=="ok"' >/dev/null; }
# start_gate : (re)start the lender's token gate on node-e
start_gate() {
  ex node-e "[ -f /run/gate.pid ] && kill \$(cat /run/gate.pid) 2>/dev/null; nohup python3 /opt/meshlive/circlegate.py $IPE $LINK_PORT >/tmp/gate.log 2>&1 & echo \$! > /run/gate.pid"
  wait_for 10 "node-e's gate listens on $IPE:$LINK_PORT" ex node-e "ss -ltn | grep -q '$IPE:$LINK_PORT '"
}

# swap_gate_for_raw / swap_raw_for_gate : on node-e, replace the token gate on the circle port by a plain
# streaming server (container/tcpserve.py, no token check, no look at /circle-peers). With it, an established
# connection can only be cut by the sidecar and its packet filter, never by the application noticing.
swap_gate_for_raw() {
  ex node-e "[ -f /run/gate.pid ] && kill \$(cat /run/gate.pid) 2>/dev/null; rm -f /run/gate.pid; true" >/dev/null
  wait_for 10 "the gate has let go of $IPE:$LINK_PORT" gate_port_free
  srv node-e "$IPE" $LINK_PORT
}
gate_port_free() { ! ex node-e "ss -ltn | grep -q '$IPE:$LINK_PORT '"; }
swap_raw_for_gate() { srv_stop node-e $LINK_PORT; sleep 1; start_gate; }
raw_works() { [ "$(link_get "$TOKEN" 5)" = "served-by=node-e bound=$IPE client=$IPA" ]; }
# stream_start SECONDS : node-a opens a streaming request to the lender's circle port in the background
stream_start() {
  ex node-a "rm -f /tmp/stream.out /tmp/stream.done; nohup sh -c 'curl -s -N -m $(( $1 + 20 )) --interface $IPA http://$IPE:$LINK_PORT/ticks/$1 > /tmp/stream.out 2>/dev/null; date +%s > /tmp/stream.done' >/dev/null 2>&1 &" >/dev/null
}
stream_ticks() { ex node-a "grep -c tick /tmp/stream.out || true" | tr -d '\r'; }

# pair_with TOKEN NODE NAME : enrol a node with a given pairing token
pair_with() {
  local r; r=$(api "$2" POST /pair "{\"pairing_token\":\"$1\",\"node_name\":\"$3\"}")
  printf '%s' "$r" | J -e '.node_id and .mesh_ip' >/dev/null || fail "pairing $3 failed: $(printf '%s' "$r" | trim | cut -c1-200)"
  ok "paired $3 as $(printf '%s' "$r" | J -r .mesh_ip)"
}

# make_link : the create call as the requester's engine makes it, from node-a over the container network.
# Sets LCODE, LBODY, LINK and TOKEN.
make_link() {
  local out
  out=$(dc exec -T node-a curl -s -m 25 -X POST -H 'Content-Type: application/json' \
    -d "{\"requester_node_id\":\"$NODEA_ID\",\"model_slug\":\"$LINK_MODEL\"}" -w '\n%{http_code}' \
    "http://coordinator:8400/me/circles/$CIRCLE/links" </dev/null)
  LCODE=${out##*$'\n'}; LBODY=${out%$'\n'*}
  LINK=$(printf '%s' "$LBODY" | J -r '.link_id // empty'); TOKEN=$(printf '%s' "$LBODY" | J -r '.token // empty')
}
# end_link [ID] : DELETE; prints the HTTP status
end_link() {
  dc exec -T node-a curl -s -m 20 -X DELETE -o /dev/null -w '%{http_code}' "http://coordinator:8400/me/circles/$CIRCLE/links/${1:-$LINK}" </dev/null
}
# renew_link [ID] : POST renew; sets RCODE and RBODY
renew_link() {
  local out; out=$(dc exec -T node-a curl -s -m 20 -X POST -H 'Content-Type: application/json' -d '{}' -w '\n%{http_code}' \
    "http://coordinator:8400/me/circles/$CIRCLE/links/${1:-$LINK}/renew" </dev/null)
  RCODE=${out##*$'\n'}; RBODY=${out%$'\n'*}
}
# link_get TOKEN [TIMEOUT] : an HTTP GET to the lender's circle port from node-a's mesh address with that bearer token
link_get() {
  ex node-a "curl -s -m ${2:-8} --interface $IPA -H 'Authorization: Bearer $1' http://$IPE:$LINK_PORT/ || echo FAILED"
}
link_works() { [ "$(link_get "$TOKEN" 5)" = "gate-ok client=$IPA link=$LINK" ]; }
link_dead()  { [ "$(link_get "$TOKEN" 4)" = FAILED ]; }
# ensure_link : a link that is up with at least 25 seconds left (renew it, or make a new one)
ensure_link() {
  local tries=0
  if [ -n "$LINK" ]; then
    renew_link
    if [ "$RCODE" = 200 ]; then wait_for 20 "the renewed link answers" link_works; return; fi
  fi
  make_link
  # A lender that just re-attested (after a coordinator restart) is not offered for a few seconds.
  while [ "$LCODE" = 409 ] && [ $tries -lt 12 ]; do tries=$((tries+1)); sleep 3; make_link; done
  [ "$LCODE" = 201 ] || fail "could not make a link: HTTP $LCODE $LBODY"
  wait_for 30 "the link answers with its token" link_works
}

# seed_household2 : the second household as rows (member M2, households row at 100.64.1.0/24, two pairing
# tokens), then node-e and node-f enrol through the real POST /pair. Sets M2. Used by phase L0 and phase RH.
seed_household2() {
  M2=$(ex stun 'cat /proc/sys/kernel/random/uuid' | tr -d '\r')
  expect_match "member 2 id" '^[0-9a-f-]{36}$' "$M2"
  local hh2 t1 t2
  hh2=$(psql -t -A -c "insert into households (owner_member_id, name, mesh_cidr) values ('$M2', 'meshlive lenders', '100.64.1.0/24') returning id" | head -1 | tr -d '\r')
  expect_match "household 2 row" '^[0-9a-f-]{36}$' "$hh2"
  t1=$(ex stun "python3 -c 'import secrets,base64;print(base64.b32encode(secrets.token_bytes(20)).decode())'" | tr -d '\r')
  t2=$(ex stun "python3 -c 'import secrets,base64;print(base64.b32encode(secrets.token_bytes(20)).decode())'" | tr -d '\r')
  psql -c "insert into pairing_tokens (token, household_id, issued_by_member_id, expires_at) values ('$t1','$hh2','$M2', now() + interval '1 hour'), ('$t2','$hh2','$M2', now() + interval '1 hour')" >/dev/null \
    || fail "could not seed the pairing tokens"
  pair_with "$t1" node-e node-e; pair_with "$t2" node-f node-f
}

dump_links() {
  printf '\n--- circle state (for diagnosis) ---\n'
  local n
  for n in node-a node-e; do
    printf '[%s] /circle-peers: ' "$n"; cpeers "$n" 2>/dev/null | J -c '.peers' 2>/dev/null || echo '(not answering)'
    printf '[%s] circle log lines:\n' "$n"; rlines "$n" 'circle link|restricted peer|tunnel filter' 2>/dev/null | tail -8
  done
  printf '[lender-engine] '; dc logs --no-log-prefix --tail 5 lender-engine 2>&1 | tail -5
}

# ---------------------------------------------------------------- L0
phase_L0() {
  phase L0 "Two households, a circle and a lender's stand-in engine [policy: $LINK_POLICY]"
  step "start the stack (coordinator with circle links on, ttl ${LINK_TTL_S}s, direct=${LINK_DIRECT}; relay: ${LINK_DERP_URL:-none})"
  docker run --rm --cap-add NET_ADMIN --device /dev/net/tun --entrypoint test "meshlive-sidecar:$VARIANT" -c /dev/net/tun \
    || fail "Docker cannot give a container /dev/net/tun with NET_ADMIN"
  dc down -v --remove-orphans >/dev/null 2>&1
  local svc="postgres coordinator-migrate coordinator stun node-a node-b node-e node-f"
  [ -n "$LINK_DERP_URL" ] && svc="$svc derp"
  dc up -d $svc >"$H/out/up-links.log" 2>&1 \
    || { tail -n 30 "$H/out/up-links.log"; dc logs --tail 30 coordinator-migrate coordinator 2>&1 | tail -n 40; fail "the stack did not come up (log: out/up-links.log)"; }
  expect_eq "coordinator answers /health" ok "$(coord GET /health | J -r '.status // empty')"
  expect_eq "docker network is internal" true "$(docker network inspect "${PROJECT}_meshnet" -f '{{.Internal}}')"
  expect_eq "the network uses the fixed TEST-NET-2 subnet, so reported endpoints look public" 198.51.100.0/24 "$(docker network inspect "${PROJECT}_meshnet" -f '{{(index .IPAM.Config 0).Subnet}}')"
  expect_nomatch "no container publishes a port to the host" '->' "$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Ports}}' | tr -d '\n')"
  expect_match "node-a cannot reach the internet" ' exit=[1-9][0-9]*$' "$(ex node-a 'curl -s -m 4 -o /dev/null -w "%{http_code}" https://example.com; echo " exit=$?"')"

  step "household 1 (node-a, node-b): the real endpoints, as the dev stub member"
  local n
  for n in node-a node-b node-e node-f; do ex "$n" 'mesh-ctl start' >/dev/null; done
  expect_match "household created" '^[0-9a-f-]{36}$' "$(coord POST /mesh/households '{"name":"meshlive"}' | J -r '.id // empty')"
  pair node-a node-a; pair node-b node-b

  step "household 2 (node-e, node-f): in development every HTTP caller is the one stub member, so this household, its two pairing tokens and the circle membership are rows"
  seed_household2
  for n in node-a node-b node-e node-f; do wait_for 90 "$n joined" joined "$n"; done
  IPA=$(meship node-a); IPB=$(meship node-b); IPE=$(meship node-e); IPF=$(meship node-f)
  expect_match "household 1 addresses are in 100.64.0.0/24" '^100\.64\.0\.[0-9]+ 100\.64\.0\.[0-9]+$' "$IPA $IPB"
  expect_match "household 2 addresses are in 100.64.1.0/24" '^100\.64\.1\.[0-9]+ 100\.64\.1\.[0-9]+$' "$IPE $IPF"
  NODEA_ID=$(api node-a GET /status | J -r .node_id); NODEE_ID=$(api node-e GET /status | J -r .node_id)
  DUMP_NODES="node-a node-b node-e node-f"
  if [ -n "$LINK_DERP_URL" ]; then
    for n in node-a node-b node-e node-f; do wait_for 60 "$n is connected to the relay" ex "$n" "mesh-ctl log-since-last-start | grep -q 'derp: auth_ok received'"; done
  fi
  wait_for 60 "node-a sees node-b reachable" peer_reachable node-a node-b
  wait_for 60 "node-b sees node-a reachable" peer_reachable node-b node-a
  wait_for 60 "node-e sees node-f reachable" peer_reachable node-e node-f
  wait_for 60 "node-f sees node-e reachable" peer_reachable node-f node-e
  expect_eq "node-a's household is node-b only" node-b "$(peer_names node-a)"
  expect_eq "node-e's household is node-f only" node-f "$(peer_names node-e)"

  step "the circle: created over HTTP by the stub member; member 2 joins by a row"
  # The coordinator's circle endpoints read display names from a table the API service owns and a coordinator-only
  # database does not have. An empty one of the right shape is enough: no row is needed for a member without a profile.
  psql -c "create table if not exists members (id uuid primary key, display_name text, email text)" >/dev/null || fail "could not create the members stand-in table"
  local cresp; cresp=$(coord POST /me/circles '{"name":"meshlive circle"}')
  CIRCLE=$(printf '%s' "$cresp" | J -r '.id // empty')
  [ -n "$CIRCLE" ] || fail "creating the circle failed: $(printf '%s' "$cresp" | cut -c1-300)"
  expect_match "circle id" '^[0-9a-f-]{36}$' "$CIRCLE"
  psql -c "insert into circle_memberships (circle_id, member_id, role) values ('$CIRCLE', '$M2', 'member')" >/dev/null || fail "could not add member 2 to the circle"
  ok "member 2 is in the circle"

  step "servers: household servers on 8080, listeners that log what they accept, the lender's gate, and the lender's stand-in engine"
  srv node-a "$IPA"; srv node-b "$IPB"; srv node-e "$IPE"; srv node-f "$IPF"
  acceptlog node-a "$IPA" 8081; acceptlog node-e "$IPE" 8081; acceptlog node-f "$IPF" 8081; acceptlog node-a "$IPA" $LINK_PORT
  start_gate
  export LENDER_MEMBER_ID=$M2 LENDER_NODE_ID=$NODEE_ID
  dc --profile engine up -d lender-engine >"$H/out/up-engine.log" 2>&1 || { tail -n 20 "$H/out/up-engine.log"; fail "the lender engine did not start"; }
  wait_for 40 "the coordinator accepted the lender engine, bound it to node-e, and reports circle links on" engine_bound

  step "every sidecar advertises circle_link_v1 and holds no circle peer"
  for n in node-a node-b node-e node-f; do
    expect_eq "$n: /status features" '["circle_link_v1"]' "$(api $n GET /status | J -c '.features')"
    expect_eq "$n: /circle-peers is empty" '{"peers":[]}' "$(cpeers $n | J -c .)"
  done

  step "every sidecar's signal connection was opened with its own node token, the only kind the coordinator offers links on"
  for n in node-a node-b node-e node-f; do
    wait_for 30 "the coordinator's latest signal connection for $n is token-bound" signal_token_bound "$(api $n GET /status | J -r .node_id)"
  done
  phase_end
}

# ---------------------------------------------------------------- L1
phase_L1() {
  phase L1 "A link is made over HTTP and carries a request, lender to requester only [policy: $LINK_POLICY]"
  require L0
  step "baseline: household traffic, and the lender's circle port refuses a stranger"
  assert_tcp node-a node-b; assert_tcp node-b node-a; assert_tcp node-e node-f; assert_tcp node-f node-e
  assert_routes node-a "$IPB"; assert_routes node-e "$IPF"
  expect_eq "no token and no link: the lender's gate refuses" FAILED "$(ex node-a "curl -s -m 4 --interface $IPA http://$IPE:$LINK_PORT/ || echo FAILED")"
  if [ "$LINK_POLICY" = relay ]; then
    # Counting rules: in the relay-only policy no UDP may leave either sidecar for the other household's machine.
    local ipe_c ipa_c
    ipe_c=$(ex node-a "getent hosts node-e | awk '{print \$1}'" | tr -d '\r'); ipa_c=$(ex node-e "getent hosts node-a | awk '{print \$1}'" | tr -d '\r')
    ex node-a "iptables -I OUTPUT 1 -p udp -d $ipe_c -j ACCEPT" >/dev/null; ex node-e "iptables -I OUTPUT 1 -p udp -d $ipa_c -j ACCEPT" >/dev/null
    UDPC_E=$ipe_c; UDPC_A=$ipa_c
    # positive control: one datagram sent on purpose is counted, so a count of nothing later means something
    ex node-a "python3 -c \"import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('$ipe_c', 9))\"" >/dev/null
    expect_eq "the counting rule on node-a sees a datagram sent on purpose" 1 "$(udp_count node-a "$UDPC_E")"
    ex node-e "python3 -c \"import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('$ipa_c', 9))\"" >/dev/null
    expect_eq "the counting rule on node-e sees a datagram sent on purpose" 1 "$(udp_count node-e "$UDPC_A")"
  fi

  step "POST /me/circles/{id}/links from the requester's machine"
  local t0 t1; t0=$(now_ms)
  make_link
  t1=$(now_ms)
  expect_eq "the create answers 201" 201 "$LCODE"
  note "the create call took about $((t1-t0)) ms including the docker exec"
  printf '%s' "$LBODY" | J -e --arg ip "$IPE" --argjson ttl "$LINK_TTL_S" --argjson port $LINK_PORT \
    '.ready==true and .lender.mesh_ip==$ip and .lender.port==$port and .ttl_s==$ttl and .renew_after_s==($ttl/2|floor) and (.token|length)==43 and (.link_id|length)==36' >/dev/null \
    && ok "the answer names the lender's mesh address and circle port, a 43 character token, ready true, ttl $LINK_TTL_S" || fail "unexpected create answer: $(printf '%s' "$LBODY" | sed -E 's/"token":"[^"]*"/"token":"..."/')"
  printf '%s' "$LBODY" | J -e 'has("lender") and (.lender|keys|sort)==["mesh_ip","port"] and (keys|sort)==["circle_id","expires_at","lender","link_id","model_slug","ready","renew_after_s","token","ttl_s"]' >/dev/null \
    && ok "the answer has exactly the fixture's fields (no node id, member id, name or hostname of the lender)" || fail "create answer has other fields"

  step "both sidecars hold the other machine as a circle peer, and neither lists it as a household peer"
  wait_for 20 "node-a holds node-e in /circle-peers" cp_has node-a "$IPE"
  wait_for 20 "node-e holds node-a in /circle-peers" cp_has node-e "$IPA"
  local sha; sha=$(sha256_of "$TOKEN")
  cpeers node-a | J -e --arg ip "$IPE" --arg id "$LINK" --argjson port $LINK_PORT '(.peers|length)==1 and .peers[0].scope=="circle" and .peers[0].mesh_ip==$ip and .peers[0].inbound==null and .peers[0].outbound.link_id==$id and .peers[0].outbound.port==$port' >/dev/null \
    && ok "node-a: one circle peer, outbound only, link $LINK, port $LINK_PORT" || fail "node-a /circle-peers: $(cpeers node-a | J -c .)"
  cpeers node-e | J -e --arg ip "$IPA" --arg id "$LINK" --arg sha "$sha" --argjson port $LINK_PORT '(.peers|length)==1 and .peers[0].scope=="circle" and .peers[0].mesh_ip==$ip and .peers[0].outbound==null and .peers[0].inbound.link_id==$id and .peers[0].inbound.port==$port and .peers[0].inbound.token_sha256==$sha' >/dev/null \
    && ok "node-e: one circle peer, inbound only, port $LINK_PORT, token_sha256 = sha256 of the token the requester holds" || fail "node-e /circle-peers: $(cpeers node-e | J -c .)"
  expect_eq "node-a's /peers lists node-b only" node-b "$(peer_names node-a)"
  expect_eq "node-e's /peers lists node-f only" node-f "$(peer_names node-e)"
  expect_eq "node-a: peers_online_count counts the household only" 1 "$(api node-a GET /status | J -r .peers_online_count)"
  expect_eq "node-e: peers_online_count counts the household only" 1 "$(api node-e GET /status | J -r .peers_online_count)"
  expect_nomatch "/circle-peers carries no key, endpoint or name of the other machine" 'wg_public_key|endpoint|node_name|node_id|198\.51\.100' "$(cpeers node-a)$(cpeers node-e)"
  assert_scope_household node-a; assert_scope_household node-e
  assert_routes node-a "$(printf '%s\n' $IPB $IPE | sort | tr '\n' ' ' | sed 's/ *$//')"
  assert_routes node-e "$(printf '%s\n' $IPF $IPA | sort | tr '\n' ' ' | sed 's/ *$//')"

  step "the request goes machine to machine: real TCP over the tunnel, source address and token checked by the lender's gate"
  wait_for 40 "node-a sees the lender reachable" cp_reachable node-a "$IPE"
  wait_for 40 "node-e sees the requester reachable" cp_reachable node-e "$IPA"
  expect_eq "the lender's gate saw the requester's mesh address and a valid token" "gate-ok client=$IPA link=$LINK" "$(link_get "$TOKEN")"
  expect_eq "a wrong token is refused" "bad_token" "$(ex node-a "curl -s -m 5 --interface $IPA -H 'Authorization: Bearer nope' http://$IPE:$LINK_PORT/ || echo FAILED" | tr -d '\r')"
  expect_eq "no token is refused" "bad_token" "$(ex node-a "curl -s -m 5 --interface $IPA http://$IPE:$LINK_PORT/ || echo FAILED" | tr -d '\r')"
  case $LINK_POLICY in
    direct|hostile) expect_eq "node-a reaches the lender over a direct path" direct "$(cp_mode node-a "$IPE")"; expect_eq "node-e reaches the requester over a direct path" direct "$(cp_mode node-e "$IPA")" ;;
    relay)  expect_eq "node-a reaches the lender over the relay" derp "$(cp_mode node-a "$IPE")"; expect_eq "node-e reaches the requester over the relay" derp "$(cp_mode node-e "$IPA")"
            expect_eq "relay-only policy: node-a sent no UDP packet to the lender's machine since the control datagram" 1 "$(udp_count node-a "$UDPC_E")"
            expect_eq "relay-only policy: node-e sent no UDP packet to the requester's machine since the control datagram" 1 "$(udp_count node-e "$UDPC_A")" ;;
  esac
  step "logs: no key or token hash at all, and no public address of the other machine on any line about the link"
  local n other
  for n in node-a node-e; do
    if [ "$n" = node-a ]; then other=$IPE; else other=$IPA; fi
    expect_ge "$n: the log has lines about the link to check" 2 "$(rlines $n "circle link|restricted peer|tunnel: peer added.*allowed_ips=\\[$other/32\\]" | wc -l | tr -d ' ')"
    expect_eq "$n: no line about the link carries the other machine's public address" 0 "$(rlines $n "circle link|restricted peer|tunnel: peer added.*allowed_ips=\\[$other/32\\]" | grep -c '198\.51\.100\.' || true)"
    expect_eq "$n: the token hash is nowhere in the log" 0 "$(ex $n "grep -c '$sha' /var/log/mesh/mesh.log || true" | tr -d '\r')"
  done
  expect_ge "node-e logged the restricted peer being added" 1 "$(log_count node-e 'tunnel filter: restricted peer added.*in_port='$LINK_PORT)"
  expect_ge "node-a logged the restricted peer being added" 1 "$(log_count node-a 'tunnel filter: restricted peer added.*out_port='$LINK_PORT)"
  phase_end
}

# ---------------------------------------------------------------- L2
phase_L2() {
  phase L2 "The link reaches one port and nothing else, in one direction [policy: $LINK_POLICY]"
  require L1; ensure_link
  step "the requester cannot reach any other port on the lender"
  assert_src_blocked node-a "$IPE" 8080 40010 "TCP to the lender's household server on 8080 times out"
  assert_src_blocked node-a "$IPE" 8081 40011 "TCP to the lender's listener on 8081 times out"
  expect_eq "the lender's 8081 listener accepted nothing from the requester" 0 "$(accepted_from node-e 8081 "$IPA")"
  assert_src_blocked node-a "$IPE" 22 40012 "TCP to the lender's port 22 times out (no listener: a refusal would mean the packet got through)"
  step "the requester cannot send the lender UDP, ICMP, fragments or stray replies; the same packets reach a household partner"
  assert_raw_blocked "node-a to the lender: ICMP echo request" node-a icmp node-e Icmp InEchos
  assert_raw_reaches "node-a to its household partner node-b: the same ICMP" node-a icmp node-b Icmp InEchos
  assert_raw_blocked "node-a to the lender: UDP datagram" node-a udp node-e Udp NoPorts 40013 9999
  assert_raw_reaches "node-a to node-b: the same UDP datagram" node-a udp node-b Udp NoPorts 40013 9999
  assert_raw_blocked "node-a to the lender: a SYN to the granted port $LINK_PORT split into two fragments" node-a fragsyn node-e Ip ReasmReqds 40014 $LINK_PORT
  assert_raw_reaches "node-a to node-b: the same fragments" node-a fragsyn node-b Ip ReasmReqds 40014 8080
  assert_raw_blocked "node-a to the lender: stray ACK from a port nobody opened" node-a ack node-e Tcp OutRsts 40015 $LINK_PORT
  assert_raw_reaches "node-a to node-b: the same stray ACK" node-a ack node-b Tcp OutRsts 40015 8080
  step "the lender cannot open a connection to the requester, on the circle port or any other"
  assert_src_blocked node-e "$IPA" $LINK_PORT 40016 "the lender's TCP to the requester's port $LINK_PORT times out"
  assert_src_blocked node-e "$IPA" 8081 40017 "the lender's TCP to the requester's port 8081 times out"
  assert_src_blocked node-e "$IPA" 8080 40018 "the lender's TCP to the requester's household server on 8080 times out"
  expect_eq "the requester's listener on $LINK_PORT accepted nothing from the lender" 0 "$(accepted_from node-a $LINK_PORT "$IPE")"
  expect_eq "the requester's listener on 8081 accepted nothing from the lender" 0 "$(accepted_from node-a 8081 "$IPE")"
  assert_raw_blocked "node-e to the requester: ICMP echo request" node-e icmp node-a Icmp InEchos
  assert_raw_blocked "node-e to the requester: UDP datagram" node-e udp node-a Udp NoPorts 40019 9999
  step "the other household's partner machines are not reachable through the link"
  expect_nomatch "node-a has no route to the lender's household partner through the tunnel" ' dev potluck0 ' "$(ex node-a "ip route get $IPF 2>&1")"
  assert_tcp_blocked node-a "$IPF" "node-a cannot reach node-f (the lender's partner)" 8080 4
  assert_tcp_blocked node-e "$IPB" "node-e cannot reach node-b (the requester's partner)" 8080 4
  note "a hostile requester adds its own route to node-f through the tunnel"
  ex node-a "ip route add $IPF/32 dev potluck0" >/dev/null
  assert_src_blocked node-a "$IPF" 8081 40020 "with a route to node-f via the tunnel, the connection still times out"
  expect_eq "node-f's listener accepted nothing from the requester" 0 "$(accepted_from node-f 8081 "$IPA")"
  ex node-a "ip route del $IPF/32 dev potluck0" >/dev/null
  note "this shows the requester's own device has no peer for node-f's address; a requester that wrote its own WireGuard config is stopped by the lender's allowed-ips and wrong-host filter rule, which the unit tests and phase F cover, not this phase"
  step "meanwhile the one allowed thing still works"
  expect_eq "the circle port still answers" "gate-ok client=$IPA link=$LINK" "$(link_get "$TOKEN")"
  phase_end
}

# ---------------------------------------------------------------- L3
phase_L3() {
  phase L3 "Household traffic on both sides is unaffected while a link is up [policy: $LINK_POLICY]"
  require L1; ensure_link
  local d0a d0e; d0a=$(log_count node-a 'tunnel filter: dropped packets'); d0e=$(log_count node-e 'tunnel filter: dropped packets')
  step "TCP both ways inside each household, with the link up"
  assert_tcp node-a node-b; assert_tcp node-b node-a; assert_tcp node-e node-f; assert_tcp node-f node-e
  step "UDP and ICMP still pass between household partners (the filter restricts only the circle peer)"
  assert_raw_reaches "node-a to node-b: ICMP" node-a icmp node-b Icmp InEchos
  assert_raw_reaches "node-b to node-a: UDP" node-b udp node-a Udp NoPorts 40021 9999
  assert_raw_reaches "node-e to node-f: ICMP" node-e icmp node-f Icmp InEchos
  assert_raw_reaches "node-f to node-e: UDP" node-f udp node-e Udp NoPorts 40022 9999
  step "bulk transfers inside each household, each checked for length; circle requests meanwhile"
  local r1 r2 r3 i bytes=40000000
  # With a relay configured the households' own peers use it too, and it is rate limited, so the transfer is smaller.
  [ -n "$LINK_DERP_URL" ] && bytes=8000000
  # Household 1's transfer runs in the background while the requester makes circle requests; household 2's runs after.
  local rf; rf=$(mktemp "${TMPDIR:-/tmp}/meshlive-bulk.XXXXXX")
  ( bulk node-b "$IPA" 8080 $bytes > "$rf" ) &
  local bgpid=$!
  for i in 1 2 3 4 5; do expect_eq "circle request $i while household 1 is transferring" "gate-ok client=$IPA link=$LINK" "$(link_get "$TOKEN")"; done
  wait $bgpid; r1=$(cat "$rf"); rm -f "$rf"
  r2=$(bulk node-f "$IPE" 8080 $bytes)
  [ "$r1" != FAILED ] && [ -n "$r1" ] && ok "node-b downloaded $((bytes/1000000)) MB from node-a (${r1#* } MB/s)" || fail "household 1 bulk transfer failed ('$r1')"
  [ "$r2" != FAILED ] && ok "node-f downloaded $((bytes/1000000)) MB from node-e (${r2#* } MB/s)" || fail "household 2 bulk transfer failed"
  step "household state: routes, peers and the filter's drop log"
  assert_routes node-a "$(printf '%s\n' $IPB $IPE | sort | tr '\n' ' ' | sed 's/ *$//')"
  assert_routes node-e "$(printf '%s\n' $IPF $IPA | sort | tr '\n' ' ' | sed 's/ *$//')"
  expect_eq "node-a's /peers is node-b only" node-b "$(peer_names node-a)"; expect_eq "node-e's /peers is node-f only" node-f "$(peer_names node-e)"
  assert_scope_household node-a; assert_scope_household node-e
  expect_eq "node-a's packet filter dropped no mesh-range packet during these checks" 0 "$(( $(log_count node-a 'tunnel filter: dropped packets') - d0a ))"
  expect_eq "node-e's packet filter dropped no mesh-range packet during these checks" 0 "$(( $(log_count node-e 'tunnel filter: dropped packets') - d0e ))"
  expect_eq "no roster entry was dropped on either side" 0 "$(( $(log_count node-a 'roster: dropping peer') + $(log_count node-e 'roster: dropping peer') ))"
  phase_end
}

# ---------------------------------------------------------------- L4
phase_L4() {
  phase L4 "Renewing extends the link on both sidecars [policy: $LINK_POLICY]"
  require L1; ensure_link
  step "let the link age, then renew"
  sleep 12
  local ea0 ee0 x0a x0e; ea0=$(cp_expires node-a "$IPE"); ee0=$(cp_expires node-e "$IPA")
  note "seconds left before the renewal: node-a $ea0, node-e $ee0"
  renew_link
  expect_eq "renew answers 200" 200 "$RCODE"
  printf '%s' "$RBODY" | J -e --arg id "$LINK" '.link_id==$id and .delivered.requester==true and .delivered.lender==true and .renew_after_s>0' >/dev/null \
    && ok "renew says both sidecars acknowledged the extension" || fail "renew answer: $RBODY"
  local ea1 ee1; ea1=$(cp_expires node-a "$IPE"); ee1=$(cp_expires node-e "$IPA")
  note "seconds left after: node-a $ea1, node-e $ee1"
  [ "$ea1" -gt "$ea0" ] && ok "node-a's deadline moved out ($ea0 -> $ea1)" || fail "node-a's deadline did not move ($ea0 -> $ea1)"
  [ "$ee1" -gt "$ee0" ] && ok "node-e's deadline moved out ($ee0 -> $ee1)" || fail "node-e's deadline did not move ($ee0 -> $ee1)"
  expect_ge "node-a's expires_in_s is near the full ttl again" $((LINK_TTL_S-8)) "$ea1"
  expect_eq "the link still carries a request" "gate-ok client=$IPA link=$LINK" "$(link_get "$TOKEN")"
  expect_eq "renewing again at once is refused with 429 (too soon)" 429 "$(renew_link; echo $RCODE)"
  phase_end
}

# ---------------------------------------------------------------- L5
phase_L5() {
  phase L5 "DELETE removes the peer, the route and the entry on both sides, and cuts a request in flight [policy: $LINK_POLICY]"
  require L1; ensure_link
  local rm_a0 rm_e0; rm_a0=$(log_count node-a 'tunnel filter: restricted peer removed'); rm_e0=$(log_count node-e 'tunnel filter: restricted peer removed')
  step "a request is streaming over the link to a server that checks nothing (the cut can only come from the sidecars)"
  swap_gate_for_raw
  wait_for 20 "the plain server answers over the link" raw_works
  stream_start 100
  sleep 3
  local n0; n0=$(stream_ticks); sleep 3; local n1; n1=$(stream_ticks)
  [ "$n1" -gt "$n0" ] && ok "ticks are arriving ($n0 then $n1)" || fail "the stream is not flowing ($n0 then $n1)"
  step "DELETE the link"
  local nd; nd=$(stream_ticks)
  expect_eq "DELETE answers 204" 204 "$(end_link)"
  sleep 4
  local n2 n3; n2=$(stream_ticks); sleep 4; n3=$(stream_ticks)
  [ "$n2" -le $((nd+5)) ] && ok "at most 5 more ticks arrived after DELETE returned ($nd before, $n2 four seconds after)" || fail "the stream kept flowing after DELETE ($nd before, $n2 four seconds after)"
  [ "$n3" = "$n2" ] && ok "and none arrived in the next 4 seconds ($n3 in all; the request was set to run 100 s)" || fail "ticks kept arriving after the cut ($n2 then $n3)"
  ex node-a "pkill -x curl; true" >/dev/null
  swap_raw_for_gate
  local t0 t1; t0=$(now_ms)
  wait_for 20 "node-a holds no circle peer" cp_empty node-a
  wait_for 20 "node-e holds no circle peer" cp_empty node-e
  t1=$(now_ms); note "both sidecars were empty $((t1-t0)) ms after DELETE returned (polling resolution about a second)"
  assert_routes node-a "$IPB"; assert_routes node-e "$IPF"
  expect_ge "node-a removed the restricted peer's filter entry" 1 "$(( $(log_count node-a 'tunnel filter: restricted peer removed') - rm_a0 ))"
  expect_ge "node-e removed the restricted peer's filter entry" 1 "$(( $(log_count node-e 'tunnel filter: restricted peer removed') - rm_e0 ))"
  expect_nomatch "node-a has no route to the lender" ' dev potluck0 ' "$(ex node-a "ip route get $IPE 2>&1")"
  expect_nomatch "node-e has no route to the requester" ' dev potluck0 ' "$(ex node-e "ip route get $IPA 2>&1")"
  expect_eq "the circle port no longer answers" FAILED "$(link_get "$TOKEN" 4)"
  expect_eq "DELETE again is 204 (idempotent)" 204 "$(end_link)"
  step "household traffic is unaffected"
  assert_tcp node-a node-b; assert_tcp node-b node-a; assert_tcp node-e node-f; assert_tcp node-f node-e
  LINK=; TOKEN=
  phase_end
}

# ---------------------------------------------------------------- L6
phase_L6() {
  phase L6 "Expiry on the sidecars' own clock, with the coordinator stopped [policy: $LINK_POLICY]"
  require L1
  LINK=; TOKEN=; ensure_link
  step "a request is streaming over the link to a server that checks nothing: only the sidecars can end it at the deadline"
  swap_gate_for_raw
  wait_for 20 "the plain server answers over the link" raw_works
  stream_start $((LINK_TTL_S+60))
  sleep 3
  step "stop the coordinator container (the sidecars' signal channel closes; no remove message can arrive)"
  dc stop coordinator >/dev/null 2>&1
  local t0=$SECONDS left; left=$(cp_expires node-a "$IPE")
  expect_eq "the coordinator is stopped" "" "$(dc ps -q --status running coordinator)"
  expect_eq "the link still answers while the coordinator is down and the grant is live" "served-by=node-e bound=$IPE client=$IPA" "$(link_get "$TOKEN")"
  note "seconds left on node-a when the coordinator stopped: about $left"
  step "wait for the deadline; poll both sidecars"
  local gone=
  while [ $((SECONDS-t0)) -lt $((LINK_TTL_S+40)) ]; do
    if cp_empty node-a && cp_empty node-e; then gone=$((SECONDS-t0)); break; fi
    sleep 1
  done
  [ -n "$gone" ] && ok "both sidecars dropped the peer on their own, ${gone}s after the coordinator stopped (the link had about ${left}s left)" || fail "a sidecar still holds the peer ${LINK_TTL_S}s+ later"
  [ "$gone" -ge $((left-5)) ] && ok "and not before the deadline" || fail "the peer was dropped ${gone}s in, long before its deadline (${left}s left)"
  [ "$gone" -le $((left+8)) ] && ok "and within 8s after it" || fail "the peer outlived its deadline by more than 8s (${gone}s in, ${left}s left)"
  # /circle-peers stops listing a grant the moment its deadline passes; the sweep that removes the route runs up to a second later.
  wait_for 10 "node-a's routes are the household's again" routes_are node-a "$IPB"
  wait_for 10 "node-e's routes are the household's again" routes_are node-e "$IPF"
  expect_nomatch "node-a has no route to the lender" ' dev potluck0 ' "$(ex node-a "ip route get $IPE 2>&1")"
  expect_eq "the circle port does not answer" FAILED "$(link_get "$TOKEN" 4)"
  local tk tk2; tk=$(stream_ticks); sleep 4; tk2=$(stream_ticks)
  [ "$tk2" = "$tk" ] && ok "the request that was streaming at the deadline has stopped receiving ($tk ticks in all; it was set to run $((LINK_TTL_S+60))s)" || fail "ticks were still arriving after the deadline ($tk then $tk2)"
  [ "$tk" -le $((left+6)) ] && ok "and it received no more than $((left+6)) ticks in total, so the data stopped at the deadline, not later" || fail "the stream delivered $tk ticks, more than the ${left}s the link had left"
  ex node-a "pkill -x curl; true" >/dev/null
  swap_raw_for_gate
  expect_ge "node-a logged the expiry" 1 "$(log_count node-a 'circle link: expired on this machine.s clock')"
  expect_ge "node-e logged the expiry" 1 "$(log_count node-e 'circle link: expired on this machine.s clock')"
  step "household traffic needs no coordinator and is unaffected"
  assert_tcp node-a node-b; assert_tcp node-b node-a; assert_tcp node-e node-f; assert_tcp node-f node-e
  step "restart the coordinator; the sidecars rejoin and the lender engine re-attests"
  local acc0 w0a w0e; acc0=$(engine_accepts); w0a=$(log_count node-a 'mesh welcome received'); w0e=$(log_count node-e 'mesh welcome received')
  dc start coordinator >/dev/null 2>&1
  wait_for 90 "the coordinator is up and healthy" coordinator_up
  # /status keeps saying "connected" through an outage, so the welcome is what shows a sidecar is back (its retry backoff reaches a minute)
  wait_for 150 "node-a reconnected (a new welcome)" log_gt node-a 'mesh welcome received' "$w0a"
  wait_for 150 "node-e reconnected (a new welcome)" log_gt node-e 'mesh welcome received' "$w0e"
  wait_for 40 "the lender engine is accepted again" engine_newer_than "$acc0"
  LINK=; TOKEN=
  phase_end
}

# ---------------------------------------------------------------- L7
phase_L7() {
  phase L7 "A coordinator restart (a fresh welcome) keeps the link [policy: $LINK_POLICY]"
  require L1; LINK=; TOKEN=; ensure_link
  sleep $((LINK_TTL_S/10+2)); renew_link; expect_eq "renew before the restart (the coordinator refuses one sooner than a tenth of the lifetime after the last)" 200 "$RCODE"
  local w0a w0e g0a g0e; w0a=$(log_count node-a 'mesh welcome received'); w0e=$(log_count node-e 'mesh welcome received')
  g0a=$(log_count node-a 'circle link: grant updated'); g0e=$(log_count node-e 'circle link: grant updated')
  step "restart the coordinator container: every sidecar reconnects and gets a welcome (SetPeers replaces the device's peer set)"
  dc restart coordinator 2>&1 | tail -n 2
  wait_for 120 "node-a applied another welcome" log_gt node-a 'mesh welcome received' "$w0a"
  wait_for 120 "node-e applied another welcome" log_gt node-e 'mesh welcome received' "$w0e"
  wait_for 30 "node-a applied it (peers configured)" welcome_applied node-a
  note "node-a: $(rlines node-a 'tunnel: peers configured' | tail -1)"
  note "node-e: $(rlines node-e 'tunnel: peers configured' | tail -1)"
  step "the circle peers are still there, once each, and the request still works"
  expect_eq "node-a still holds one circle peer" 1 "$(cp_count node-a)"
  expect_eq "node-e still holds one circle peer" 1 "$(cp_count node-e)"
  wait_for 60 "node-a sees the lender reachable" cp_reachable node-a "$IPE"
  wait_for 60 "node-e sees the requester reachable" cp_reachable node-e "$IPA"
  wait_for 40 "the same token still gets through the lender's gate" link_works
  expect_eq "the circle peer was never listed as a household peer" node-b "$(peer_names node-a)"
  note "the coordinator replays live links on reconnect; grant-updated lines since: node-a +$(( $(log_count node-a 'circle link: grant updated') - g0a )), node-e +$(( $(log_count node-e 'circle link: grant updated') - g0e ))"
  step "household traffic"
  assert_tcp node-a node-b; assert_tcp node-b node-a; assert_tcp node-e node-f; assert_tcp node-f node-e
  phase_end
}

# ---------------------------------------------------------------- L8
phase_L8() {
  phase L8 "Removing the lender's membership tears the link down within seconds [policy: $LINK_POLICY]"
  require L1
  LINK=; TOKEN=; ensure_link
  local mid; mid=$(psql -t -A -c "select id from circle_memberships where circle_id='$CIRCLE' and member_id='$M2'" | head -1 | tr -d '\r')
  expect_match "the lender's membership row" '^[0-9a-f-]{36}$' "$mid"
  step "the circle owner removes the lender's membership over HTTP"
  local code; code=$(dc exec -T node-a curl -s -m 20 -X DELETE -o /dev/null -w '%{http_code}' "http://coordinator:8400/me/circles/$CIRCLE/people/$mid" </dev/null)
  expect_eq "removing the person answers 204" 204 "$code"
  local t0 t1; t0=$(now_ms)
  wait_for 20 "node-a holds no circle peer" cp_empty node-a
  wait_for 20 "node-e holds no circle peer" cp_empty node-e
  t1=$(now_ms); note "both sidecars were empty $((t1-t0)) ms after the removal returned (polling resolution about a second)"
  [ $((t1-t0)) -le 8000 ] && ok "within seconds" || fail "the teardown took $((t1-t0)) ms"
  expect_eq "the circle port no longer answers" FAILED "$(link_get "$TOKEN" 4)"
  renew_link; expect_eq "renewing the ended link is 410 link_ended" 410 "$RCODE"
  printf '%s' "$RBODY" | J -e '.detail.code=="link_ended" and .detail.reason=="member_removed"' >/dev/null && ok "with reason member_removed" || fail "renew body: $RBODY"
  assert_routes node-a "$IPB"; assert_routes node-e "$IPF"
  step "household traffic"
  assert_tcp node-a node-b; assert_tcp node-b node-a; assert_tcp node-e node-f; assert_tcp node-f node-e
  psql -c "insert into circle_memberships (circle_id, member_id, role) values ('$CIRCLE', '$M2', 'member')" >/dev/null || fail "could not restore the membership"
  LINK=; TOKEN=
  phase_end
}

# ---------------------------------------------------------------- L9
phase_L9() {
  phase L9 "The lender's sidecar restarts mid-link; the coordinator's replay restores the link [policy: $LINK_POLICY]"
  require L1; LINK=; TOKEN=; ensure_link
  step "restart node-e's sidecar process"
  local pa; pa=$(sidecar_pid node-a)
  restart_sidecar node-e
  expect_eq "node-a was not restarted" "$pa" "$(sidecar_pid node-a)"
  step "the coordinator replays the live link to node-e on reconnect"
  wait_for 40 "node-e holds the circle peer again" cp_has node-e "$IPA"
  wait_for 60 "node-e sees the requester reachable" cp_reachable node-e "$IPA"
  srv node-e "$IPE"; start_gate
  wait_for 60 "the same token works again" link_works
  assert_tcp node-e node-f; assert_tcp node-f node-e; assert_tcp node-a node-b
  phase_end
}

# ---------------------------------------------------------------- LT
phase_LT() {
  phase LT "Link setup time: POST to the first byte of the lender's answer [policy: $LINK_POLICY]"
  require L1
  local runs=${LINK_RUNS:-8} i mode out ms cms rms ok_n=0
  local engine_ms="" naive_ms="" creates="" readies=""
  note "each sample: a new link made and used by container/linkprobe.py on node-a, then deleted. $runs samples in engine mode (wait for /circle-peers to say reachable, then connect) and $runs in naive mode (connect at once and let TCP retry)"
  if [ -n "$LINK" ]; then end_link >/dev/null; LINK=; TOKEN=; sleep 3; fi
  for mode in engine naive; do
    for i in $(seq 1 "$runs"); do
      out=$(ex node-a "python3 /opt/meshlive/linkprobe.py http://coordinator:8400 $CIRCLE $NODEA_ID $LINK_MODEL $IPA 25 $mode" | tail -1)
      ms=$(printf '%s' "$out" | J -r '.first_byte_ms // empty'); cms=$(printf '%s' "$out" | J -r '.create_ms // empty'); rms=$(printf '%s' "$out" | J -r '.ready_ms // empty')
      if [ -z "$ms" ]; then printf '%s sample %s: no answer: %s\n' "$mode" "$i" "$(printf '%s' "$out" | sed -E 's/"token":"[^"]*"/"token":"..."/' | cut -c1-200)"; else
        printf '%s sample %s: create %s ms, %sfirst byte %s ms (%s attempts)\n' "$mode" "$i" "$cms" "${rms:+ready $rms ms, }" "$ms" "$(printf '%s' "$out" | J -r .attempts)"
        ok_n=$((ok_n+1)); creates="$creates $cms"
        if [ "$mode" = engine ]; then engine_ms="$engine_ms $ms"; readies="$readies $rms"; else naive_ms="$naive_ms $ms"; fi
      fi
      end_link "$(printf '%s' "$out" | J -r '.link_id // empty')" >/dev/null
      wait_for 20 "both sidecars let go before the next sample" both_empty
      sleep 1
    done
  done
  [ "$ok_n" = $((2*runs)) ] && ok "all $((2*runs)) links carried a request" || fail "only $ok_n of $((2*runs)) links carried a request"
  LINK_TIMES="$LINK_TIMES
[$LINK_POLICY] engine mode, POST to the first byte (ms): $(printf '%s\n' $engine_ms | sort -n | tr '\n' ' ')
[$LINK_POLICY] engine mode summary: $(stats_ms $engine_ms)
[$LINK_POLICY] engine mode, POST to reachable (ms): $(stats_ms $readies)
[$LINK_POLICY] naive mode, POST to the first byte (ms): $(printf '%s\n' $naive_ms | sort -n | tr '\n' ' ')
[$LINK_POLICY] naive mode summary: $(stats_ms $naive_ms)
[$LINK_POLICY] create call alone (ms): $(stats_ms $creates)"
  printf '%s\n' "$LINK_TIMES" | tail -6
  phase_end
}

# stats_ms V1 V2... : n, min, median, mean, max and the spread (max-min)/median
stats_ms() {
  printf '%s\n' "$@" | sort -n | awk '{v[NR]=$1; s+=$1} END {m=(NR%2)?v[(NR+1)/2]:(v[NR/2]+v[NR/2+1])/2; printf "n=%d min=%d median=%d mean=%d max=%d ms, spread (max-min)/median = %.0f%%", NR, v[1], m, s/NR, v[NR], (m>0)?100*(v[NR]-v[1])/m:0}'
}

# ---------------------------------------------------------------- LD
phase_LD() {
  phase LD "Direct is tried first; with UDP between the two machines blocked the link falls back to the relay [policy: $LINK_POLICY]"
  require L1; LINK=; TOKEN=
  local ipe_c ipa_c; ipe_c=$(ex node-a "getent hosts node-e | awk '{print \$1}'" | tr -d '\r'); ipa_c=$(ex node-e "getent hosts node-a | awk '{print \$1}'" | tr -d '\r')
  step "control: with nothing blocked the link is direct"
  ensure_link
  wait_for 30 "node-a reaches the lender direct" cp_mode_is node-a "$IPE" direct
  end_link >/dev/null; LINK=; TOKEN=; wait_for 20 "node-a let go" cp_empty node-a; wait_for 20 "node-e let go" cp_empty node-e
  step "block UDP between node-a and node-e (both directions); household peers are not affected"
  ex node-a "iptables -I OUTPUT 1 -p udp -d $ipe_c -j DROP; iptables -I INPUT 1 -p udp -s $ipe_c -j DROP" >/dev/null
  ex node-e "iptables -I OUTPUT 1 -p udp -d $ipa_c -j DROP; iptables -I INPUT 1 -p udp -s $ipa_c -j DROP" >/dev/null
  local out ms rms mode; out=$(ex node-a "python3 /opt/meshlive/linkprobe.py http://coordinator:8400 $CIRCLE $NODEA_ID $LINK_MODEL $IPA 30 engine" | tail -1)
  ms=$(printf '%s' "$out" | J -r '.first_byte_ms // empty'); rms=$(printf '%s' "$out" | J -r '.ready_ms // empty')
  LINK=$(printf '%s' "$out" | J -r '.link_id // empty'); TOKEN=$(printf '%s' "$out" | J -r '.token // empty')
  [ -n "$ms" ] && ok "the request got through after ${ms} ms (reachable after ${rms} ms; direct window 3000 ms, then the relay)" || fail "no answer: $(printf '%s' "$out" | sed -E 's/"token":"[^"]*"/"token":"..."/')"
  [ "$rms" -ge 2500 ] && ok "and not before the direct window ended" || fail "reachable after ${rms} ms, before the direct window could have ended"
  wait_for 20 "node-a shows the relay as the transport" cp_mode_is node-a "$IPE" derp
  expect_ge "node-a logged the move to the relay" 1 "$(log_count node-a 'circle link: peer moved to the relay')"
  expect_ge "node-e moved to the relay or saw the requester's handshake arrive over it" 1 "$(log_count node-e 'circle link: (peer moved to the relay|handshake arrived over the relay)')"
  expect_eq "node-e shows the relay as the transport" derp "$(cp_mode node-e "$IPA")"
  assert_tcp node-a node-b; assert_tcp node-e node-f
  note "setup time with the direct path blocked, engine mode: reachable ${rms} ms, first byte ${ms} ms"
  LINK_TIMES="$LINK_TIMES
[$LINK_POLICY] direct blocked, fallback to the relay, engine mode: reachable after ${rms} ms, first byte ${ms} ms"
  end_link >/dev/null; LINK=; TOKEN=
  wait_for 20 "both sidecars let go" both_empty
  out=$(ex node-a "python3 /opt/meshlive/linkprobe.py http://coordinator:8400 $CIRCLE $NODEA_ID $LINK_MODEL $IPA 30 naive" | tail -1)
  ms=$(printf '%s' "$out" | J -r '.first_byte_ms // empty')
  LINK=$(printf '%s' "$out" | J -r '.link_id // empty'); TOKEN=$(printf '%s' "$out" | J -r '.token // empty')
  [ -n "$ms" ] && ok "a client that connects at once and lets TCP retry got through after ${ms} ms" || fail "no answer from the naive client"
  LINK_TIMES="$LINK_TIMES
[$LINK_POLICY] direct blocked, naive client: first byte ${ms} ms"
  end_link >/dev/null; LINK=; TOKEN=
  ex node-a "iptables -D OUTPUT -p udp -d $ipe_c -j DROP; iptables -D INPUT -p udp -s $ipe_c -j DROP" >/dev/null
  ex node-e "iptables -D OUTPUT -p udp -d $ipa_c -j DROP; iptables -D INPUT -p udp -s $ipa_c -j DROP" >/dev/null
  phase_end
}

# ---------------------------------------------------------------- LH
# Only in the hostile pass: node-a runs a rewritten requester (shim/packetfilter_open.go, allowed-ips widened to
# the lender's household /24), node-e runs the filter build (drop counters) and forwards IP packets.
phase_LH() {
  phase LH "A requester rewritten to leave its grant cannot get past the lender, even if the lender's host forwards [policy: $LINK_POLICY]"
  require L1; ensure_link
  step "set-up: what node-a and node-e are running"
  expect_ge "node-a runs the rewritten requester build (its own packet filter is open)" 1 "$(log_count node-a 'OPEN FILTER SHIM ACTIVE')"
  expect_ge "node-a's circle peer carries the lender household's whole /24 as allowed-ips" 1 "$(log_count node-a "tunnel: peer added.*allowed_ips=.?\\[$IPE/32 100.64.1.0/24\\]")"
  expect_eq "node-e forwards IP packets (ip_forward=1): a packet for node-f that got past the filter would be routed on" 1 "$(ex node-e 'cat /proc/sys/net/ipv4/ip_forward' | tr -d '\r')"
  expect_ge "node-e runs the filter build, so its drop counters can be read" 0 "$(fdrops node-e wrong_host)"
  expect_eq "control: what the grant allows still works from the rewritten requester" "gate-ok client=$IPA link=$LINK" "$(link_get "$TOKEN")"

  step "the rewritten requester sends to node-f, the lender's household partner, through the circle tunnel"
  ex node-a "ip route add $IPF/32 dev potluck0 2>/dev/null; ip route add 100.64.1.77/32 dev potluck0 2>/dev/null; true" >/dev/null
  local f0 w0 p0 i0 u0; f0=$(snmp node-e Ip ForwDatagrams); w0=$(fdrops node-e wrong_host)
  p0=$(snmp node-f Tcp OutRsts); i0=$(snmp node-f Icmp InEchos); u0=$(snmp node-f Udp NoPorts)
  assert_src_blocked node-a "$IPF" 8081 40030 "TCP to node-f:8081 times out"
  assert_src_blocked node-a "$IPF" 8080 40031 "TCP to node-f:8080 (a server that answers household peers) times out"
  ex node-a "python3 /opt/meshlive/rawsend.py syn $IPA $IPF 40032 8081; python3 /opt/meshlive/rawsend.py icmp $IPA $IPF; python3 /opt/meshlive/rawsend.py udp $IPA $IPF 40033 9999; python3 /opt/meshlive/rawsend.py ack $IPA 100.64.1.77 40034 8081" >/dev/null
  sleep 1
  local w1 f1; w1=$(fdrops node-e wrong_host); f1=$(snmp node-e Ip ForwDatagrams)
  [ $((w1-w0)) -ge 6 ] && ok "the packets arrived through the circle tunnel and node-e's filter dropped them as wrong_host (+$((w1-w0)))" || fail "node-e's filter counted only $((w1-w0)) wrong_host drops; the forged packets may not have reached it"
  expect_eq "node-e forwarded no datagram, though it is allowed to" 0 "$((f1-f0))"
  expect_eq "node-f's listener on 8081 accepted nothing from the requester" 0 "$(accepted_from node-f 8081 "$IPA")"
  expect_eq "node-f's kernel saw no TCP reply it would have sent to a stray SYN" 0 "$(( $(snmp node-f Tcp OutRsts) - p0 ))"
  expect_eq "node-f's kernel saw no ICMP echo from the requester" 0 "$(( $(snmp node-f Icmp InEchos) - i0 ))"
  expect_eq "node-f's kernel saw no UDP datagram from the requester" 0 "$(( $(snmp node-f Udp NoPorts) - u0 ))"

  step "the lender itself: other ports, and a port that is open to its own household"
  assert_src_blocked node-a "$IPE" 8080 40035 "TCP to the lender's household server on 8080 times out"
  assert_src_blocked node-a "$IPE" 8081 40036 "TCP to the lender's listener on 8081 times out"
  expect_eq "the lender's 8081 listener accepted nothing from the requester" 0 "$(accepted_from node-e 8081 "$IPA")"
  ex node-a "ip route del $IPF/32 dev potluck0; ip route del 100.64.1.77/32 dev potluck0; true" >/dev/null

  step "the lender's household and the circle port are unaffected"
  assert_tcp node-e node-f; assert_tcp node-f node-e
  expect_eq "the circle port still answers" "gate-ok client=$IPA link=$LINK" "$(link_get "$TOKEN")"
  phase_end
}

# ---------------------------------------------------------------- LU
# Last: it unenrols node-e.
phase_LU() {
  phase LU "POST /unpair clears the circle peers [policy: $LINK_POLICY]"
  require L1; LINK=; TOKEN=; ensure_link
  expect_ge "node-e holds the circle peer before the unpair" 1 "$(cp_count node-e)"
  step "POST /unpair on the lender's sidecar"
  expect_eq "unpair answers 204" 204 "$(ex node-e 'curl -s --unix-socket /run/potluck-mesh.sock -X POST http://localhost/unpair -o /dev/null -w "%{http_code}"')"
  expect_eq "node-e holds no circle peer" 0 "$(cp_count node-e)"
  expect_nomatch "node-e has no route to the requester" ' dev potluck0 ' "$(ex node-e "ip route get $IPA 2>&1")"
  expect_ge "node-e removed the filter entry" 1 "$(log_count node-e 'tunnel filter: restricted peer removed')"
  expect_eq "the circle port does not answer" FAILED "$(link_get "$TOKEN" 4)"
  phase_end
}
