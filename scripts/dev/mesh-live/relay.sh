#!/usr/bin/env bash
# Phase RH for WITH_RELAY=1: household peers with a relay configured. Sourced by run.sh after lib.sh and
# links.sh; not meant to be run alone. Written for bash 3.2.
#
# Why it exists: with a relay configured, the coordinator's punch_coord starts a direct-path trial on both
# machines of a pair at the same moment (DERP off, WireGuard peer removed and added again pinned to the
# direct endpoint). The default run has no relay, so it never takes that path; the circle link passes do, but
# only as a side effect. This phase takes it on purpose and measures it, with nothing but a household of two
# machines (twice over, so each stack start makes two simultaneous trials).
#
# The stack is the link stack without the circle parts: postgres, coordinator, stun, the relay built from
# services/derp, and four sidecars (node-a and node-b in household 1, node-e and node-f in household 2).
# One run is one stack start. RELAY_RUNS=N makes N of them, one after the other, and counts how many
# passed instead of stopping at the first miss, so a failure rate can be read off a single invocation.
#
# Checks per run (a miss is recorded and the next run goes on; the phase fails at the end if any run missed):
#   1. TCP in both directions inside both households, straight after both machines of a pair report the
#      other reachable. A handshake alone does not count: the exchange has to complete.
#   2. The coordinator restarts (every sidecar gets a new welcome and a new punch_coord). Four probe loops,
#      one per direction, run through it: one GET every 200 ms with a 1 s limit. The longest time without a
#      successful GET, in each direction, must stay under RELAY_MAX_OUTAGE_MS (default 5000). After the
#      probes stop, TCP in all four directions once more.
#   3. Each sidecar started at least one direct-path trial in its run and none timed out. A trial that falls
#      back to the relay is the safe outcome in production, but on this stack the direct path works, so a
#      timeout here means the two trials did not converge.

RELAY_MAX_OUTAGE_MS=${RELAY_MAX_OUTAGE_MS:-5000}
RELAY_WINDOW_S=${RELAY_WINDOW_S:-25}
RH_BAD=0

# rh_miss DESC DETAIL : a measured check that did not hold. Recorded; the run goes on.
rh_miss() { printf 'MISS  %s: %s\n' "$1" "$2"; RH_BAD=$((RH_BAD+1)); }

# rh_tcp FROM TO : one TCP exchange over the tunnel, as assert_tcp makes it, but a miss does not end the phase
rh_tcp() {
  local want out t0=$SECONDS
  want="served-by=$2 bound=$(ipof "$2") client=$(ipof "$1")"
  out=$(tcp_get "$1" "$(ipof "$2")")
  if [ "$out" = "$want" ]; then
    ok "TCP $1 -> $2 over the tunnel (took $((SECONDS-t0))s)"
  else
    rh_miss "TCP $1 -> $2 over the tunnel (took $((SECONDS-t0))s)" "expected '$want', got '$out'"
  fi
}

# rh_stack_up : a fresh stack, four paired sidecars on the relay, every pair reporting reachable
rh_stack_up() {
  local n
  dc down -v --remove-orphans >/dev/null 2>&1
  dc up -d postgres coordinator-migrate coordinator stun derp node-a node-b node-e node-f >"$H/out/up-relay.log" 2>&1 \
    || { tail -n 30 "$H/out/up-relay.log"; dc logs --tail 30 coordinator-migrate coordinator 2>&1 | tail -n 40; fail "the stack did not come up (log: out/up-relay.log)"; }
  expect_eq "coordinator answers /health" ok "$(coord GET /health | J -r '.status // empty')"
  [ -n "${RH_AFTER_UP:-}" ] && "$RH_AFTER_UP"
  for n in node-a node-b node-e node-f; do ex "$n" 'mesh-ctl start' >/dev/null; done
  expect_match "household created" '^[0-9a-f-]{36}$' "$(coord POST /mesh/households '{"name":"meshlive"}' | J -r '.id // empty')"
  pair node-a node-a; pair node-b node-b
  seed_household2
  for n in node-a node-b node-e node-f; do wait_for 90 "$n joined" joined "$n"; done
  IPA=$(meship node-a); IPB=$(meship node-b); IPE=$(meship node-e); IPF=$(meship node-f)
  for n in node-a node-b node-e node-f; do wait_for 60 "$n is connected to the relay" ex "$n" "mesh-ctl log-since-last-start | grep -q 'derp: auth_ok received'"; done
  srv node-a "$IPA"; srv node-b "$IPB"; srv node-e "$IPE"; srv node-f "$IPF"
  wait_for 60 "node-a sees node-b reachable" peer_reachable node-a node-b
  wait_for 60 "node-b sees node-a reachable" peer_reachable node-b node-a
  wait_for 60 "node-e sees node-f reachable" peer_reachable node-e node-f
  wait_for 60 "node-f sees node-e reachable" peer_reachable node-f node-e
}

# rh_probe_start FROMNODE TONODE : a probe loop inside FROMNODE's container (container/probeloop.sh)
rh_probe_start() {
  ex "$1" "rm -f /tmp/probe.stop; nohup sh /opt/meshlive/probeloop.sh $(ipof "$1") $(ipof "$2") /tmp/probe.out >/dev/null 2>&1 & echo \$! > /run/probe.pid" >/dev/null
}
# rh_probe_stop NODE : stop the loop and print "<tries> <failed tries> <longest gap between successes in ms>"
rh_probe_stop() {
  ex "$1" "touch /tmp/probe.stop; i=0; while kill -0 \$(cat /run/probe.pid) 2>/dev/null && [ \$i -lt 30 ]; do sleep 0.2; i=\$((i+1)); done; cat /tmp/probe.out" \
    | tr -d '\r' | awk '
      { n++; if ($2 == "ok") { if (prev > 0 && $1 - prev > gap) gap = $1 - prev; if (!prev) first_ok = $1; prev = $1 } else f++; last = $1; if (!first) first = $1 }
      END { if (prev > 0 && last - prev > gap) gap = last - prev; if (!prev) gap = last - first; printf "%d %d %d\n", n, f, gap }'
}

# rh_welcomed : every sidecar has applied a welcome after the counts in RH_W (set by rh_once)
rh_welcomed() {
  local i=0 n
  for n in node-a node-b node-e node-f; do
    i=$((i+1))
    [ "$(log_count "$n" 'mesh welcome received')" -gt "$(echo "$RH_W" | cut -d' ' -f$i)" ] || return 1
  done
}

# rh_wait_punch_throttle : a sidecar sends at most one request_punch per peer every 30 s. The restart below only
# brings the punch_coord (and so the trials) this phase is about when it comes after that, so wait until 32 s have
# passed since the last request_punch any of the four sidecars sent. Without the wait a restart soon after the
# stack came up runs the welcome path alone: both sidecars then send crossing initiations on welcome and the pair
# is without a session for about 5 s, until wireguard-go's retransmit timer, which is a different and older case.
rh_wait_punch_throttle() {
  local n w=0 r
  for n in node-a node-b node-e node-f; do
    r=$(ex "$n" "mesh-ctl log-since-last-start | grep 'sending request_punch' | tail -n 1 | python3 -c '
import sys, datetime, time
w = sys.stdin.read().split()
if not w:
    print(0)
else:
    t = datetime.datetime.strptime(w[0][5:], \"%Y-%m-%dT%H:%M:%S.%fZ\").replace(tzinfo=datetime.timezone.utc).timestamp()
    print(max(0, int(t + 32 - time.time()) + 1))
'" | tr -d '\r')
    [ "${r:-0}" -gt "$w" ] && w=$r
  done
  if [ "$w" -gt 0 ]; then note "waiting ${w}s so that the 30 s request_punch throttle has passed and the restart brings a new punch_coord"; sleep "$w"; fi
}

# rh_once N : one stack start, all the checks
rh_once() {
  local it=$1 n other bad0=$RH_BAD res f gap tries t0 tr0 i started since verified tout
  step "run $it: a fresh stack on the relay, four sidecars paired"
  rh_stack_up

  step "run $it: TCP in both directions inside both households, straight after both machines of a pair report reachable"
  rh_tcp node-a node-b; rh_tcp node-b node-a; rh_tcp node-e node-f; rh_tcp node-f node-e

  step "run $it: the coordinator restarts; four probe loops run through it"
  rh_wait_punch_throttle
  RH_W=$(for n in node-a node-b node-e node-f; do printf '%s ' "$(log_count "$n" 'mesh welcome received')"; done | sed 's/ $//')
  tr0=$(for n in node-a node-b node-e node-f; do printf '%s ' "$(log_count "$n" 'punch_coord: WAN direct trial started')"; done | sed 's/ $//')
  rh_probe_start node-a node-b; rh_probe_start node-b node-a; rh_probe_start node-e node-f; rh_probe_start node-f node-e
  sleep 2
  t0=$SECONDS
  dc restart coordinator 2>&1 | tail -n 2
  wait_for 120 "every sidecar applied another welcome" rh_welcomed
  note "all four welcomes applied $((SECONDS-t0))s after the restart began; probing for ${RELAY_WINDOW_S}s more"
  sleep "$RELAY_WINDOW_S"
  for n in node-a node-b node-e node-f; do
    case $n in node-a) other=node-b;; node-b) other=node-a;; node-e) other=node-f;; node-f) other=node-e;; esac
    res=$(rh_probe_stop "$n")
    tries=${res%% *}; f=${res#* }; f=${f%% *}; gap=${res##* }
    note "$n -> $other: $tries tries, $f failed, longest time without a success ${gap} ms"
    if [ "$tries" -lt 40 ]; then rh_miss "$n -> $other: the probe loop ran" "only $tries tries"
    elif [ "$gap" -le "$RELAY_MAX_OUTAGE_MS" ]; then ok "$n -> $other: no outage longer than ${RELAY_MAX_OUTAGE_MS} ms (longest ${gap} ms)"
    else rh_miss "$n -> $other: longest time without a success" "${gap} ms, limit ${RELAY_MAX_OUTAGE_MS} ms"; fi
  done
  step "run $it: TCP in all four directions after the probes"
  rh_tcp node-a node-b; rh_tcp node-b node-a; rh_tcp node-e node-f; rh_tcp node-f node-e

  step "run $it: every sidecar tried a direct path, verified it, and no trial timed out"
  i=0
  for n in node-a node-b node-e node-f; do
    i=$((i+1))
    started=$(log_count "$n" 'punch_coord: WAN direct trial started')
    verified=$(log_count "$n" 'punch_trial: VERIFIED')
    tout=$(log_count "$n" 'punch_trial: TIMEOUT')
    note "$n: trials started $started ($(( started - $(echo "$tr0" | cut -d' ' -f$i) )) since the restart), verified $verified, timed out $tout; first trial $(ex "$n" "mesh-ctl log-since-last-start | grep -m1 'WAN direct trial started' | grep -o 'starts_handshake=[a-z]*'" | tr -d '\r')"
    if [ "$started" -ge 1 ]; then ok "$n started a direct-path trial ($started)"; else rh_miss "$n: a direct-path trial" "none started, so the path under test was not taken"; fi
    since=$(( started - $(echo "$tr0" | cut -d' ' -f$i) ))
    if [ "$since" -ge 1 ]; then ok "$n started a new trial after the coordinator restart ($since)"; else rh_miss "$n: a new trial after the coordinator restart" "none: no punch_coord reached it, or it was ignored because of the backoff after an earlier timeout, so the restart ran the welcome path only"; fi
    if [ "$tout" -eq 0 ]; then ok "$n: no trial timed out"; else rh_miss "$n: trials that timed out" "$tout (the two trials did not converge; the relay took over)"; fi
  done

  if [ "$RH_BAD" -gt "$bad0" ]; then
    for n in node-a node-b node-e node-f; do ex "$n" 'mesh-ctl log-since-last-start' > "$H/out/relay-run-$it-$n.log" 2>/dev/null; done
    printf 'run %s: MISSED (%s). sidecar logs of this run: out/relay-run-%s-<node>.log\n' "$it" "$((RH_BAD-bad0))" "$it"
    return 1
  fi
  printf 'run %s: all checks held\n' "$it"
  return 0
}

phase_RH() {
  phase RH "Household peers with a relay configured: traffic right after reachable and through a coordinator restart [sidecar: ${RELAY_LABEL:-working tree}]"
  local runs=${RELAY_RUNS:-1} i passed=0 failed_runs=
  for i in $(seq 1 "$runs"); do
    if rh_once "$i"; then passed=$((passed+1)); else failed_runs="$failed_runs $i"; fi
  done
  printf '\nRH RESULT: %s of %s stack starts passed (sidecar: %s)%s\n' "$passed" "$runs" "${RELAY_LABEL:-working tree}" "${failed_runs:+; missed in runs:$failed_runs}"
  SUMMARY="${SUMMARY:-}RH result: $passed of $runs stack starts passed (sidecar: ${RELAY_LABEL:-working tree})
"
  [ "$passed" -eq "$runs" ] || fail "$passed of $runs stack starts passed; missed in runs:$failed_runs"
  phase_end
}

# ---------------------------------------------------------------- RO
# ro_rule : node-b drops every inbound WireGuard datagram except handshake messages, in the raw table so the
# rules the sidecar adds to the filter table cannot get ahead of it. A handshake initiation is 148 bytes of
# payload and a response 92; with the 20 byte IP header and the 8 byte UDP header the packets are 176 and 120.
# Handshakes complete, keepalives and data do not arrive: a session that works in one direction only.
ro_rule() {
  ex node-b 'iptables -t raw -I PREROUTING -p udp --dport 51820 -m length ! --length 120 -m length ! --length 176 -j DROP' \
    || fail "could not install the drop rule on node-b (iptables raw table with the length match)"
  expect_match "node-b has the drop rule" 'length' "$(ex node-b 'iptables -t raw -S PREROUTING')"
}

# ro_seconds NODE : seconds from this node's first "direct trial started" line to its first "NO DATA PATH" line, or none
ro_seconds() {
  ex "$1" "mesh-ctl log-since-last-start | grep -E 'WAN direct trial started|NO DATA PATH|punch_trial: TIMEOUT' | python3 -c '
import sys, datetime
t0 = t1 = None
for line in sys.stdin:
    ts = datetime.datetime.strptime(line.split()[0][5:], \"%Y-%m-%dT%H:%M:%S.%fZ\")
    if \"trial started\" in line and t0 is None: t0 = ts
    elif t0 is not None and (\"NO DATA PATH\" in line or \"TIMEOUT\" in line) and t1 is None: t1 = ts
print(\"none\" if t0 is None or t1 is None else \"%.1f\" % (t1 - t0).total_seconds())
'" | tr -d '\r'
}

phase_RO() {
  phase RO "A session that works in one direction only is caught inside the trial window and the pair goes back to the relay [sidecar: ${RELAY_LABEL:-working tree}]"
  RH_BAD=0
  step "a fresh stack on the relay; node-b drops all inbound WireGuard data from before its sidecar starts, so every handshake on the pair node-a / node-b completes and no packet reaches node-b"
  RH_AFTER_UP=ro_rule
  rh_stack_up
  RH_AFTER_UP=

  step "the unaffected pair node-e / node-f carries traffic at once"
  rh_tcp node-e node-f; rh_tcp node-f node-e

  step "node-a's packets must follow node-b onto the relay although node-b sends nothing of its own: node-a -> node-b polled alone first, then node-b -> node-a"
  local limit=${RELAY_RECOVER_S:-30} t0=$SECONDS ab=- ba=-
  while [ $((SECONDS-t0)) -lt "$limit" ]; do
    [ "$(tcp_get node-a "$IPB" 8080 2)" = "served-by=node-b bound=$IPB client=$IPA" ] && { ab=$((SECONDS-t0)); break; }
    sleep 1
  done
  while [ $((SECONDS-t0)) -lt "$limit" ]; do
    [ "$(tcp_get node-b "$IPA" 8080 2)" = "served-by=node-a bound=$IPA client=$IPB" ] && { ba=$((SECONDS-t0)); break; }
    sleep 1
  done
  note "first TCP exchange after the pair reported reachable: node-a -> node-b ${ab}s, node-b -> node-a ${ba}s (- means never within ${limit}s)"
  [ "$ab" != - ] && ok "TCP node-a -> node-b works ${ab}s after both reported reachable" || rh_miss "TCP node-a -> node-b" "no exchange within ${limit}s: node-a's outbound stayed on the dead direct path"
  [ "$ba" != - ] && ok "TCP node-b -> node-a works ${ba}s after both reported reachable" || rh_miss "TCP node-b -> node-a" "no exchange within ${limit}s"
  sleep 5
  rh_tcp node-a node-b; rh_tcp node-b node-a

  step "node-a verified on node-b's packets and stayed; node-b found no evidence, added the peer again once, then ended with NO DATA PATH inside the trial window"
  expect_ge "node-a was verified by an inbound packet from node-b" 1 "$(log_count node-a 'punch_trial: VERIFIED.*verified_by=inbound')"
  expect_eq "node-a logged no NO DATA PATH" 0 "$(log_count node-a 'NO DATA PATH')"
  expect_eq "node-a did not add the peer again" 0 "$(log_count node-a 'adding the peer again once')"
  expect_ge "node-b added the peer again once" 1 "$(log_count node-b 'adding the peer again once')"
  expect_ge "node-b ended with NO DATA PATH (the handshake was fresh, so not a plain timeout)" 1 "$(log_count node-b 'NO DATA PATH')"
  expect_eq "node-b was not verified" 0 "$(log_count node-b 'punch_trial: VERIFIED')"
  local sb; sb=$(ro_seconds node-b)
  note "seconds from the trial's start to its end: node-b ${sb}"
  case $sb in none) rh_miss "node-b's trial ended" "it did not, so the relay stayed off with the direct path dead" ;;
    *) awk -v v="$sb" 'BEGIN{exit !(v <= 18)}' && ok "node-b's trial ended ${sb}s after it started (the window is 15s)" || rh_miss "node-b's trial ended" "after ${sb}s, the window is 15s" ;; esac
  step "the unaffected pair verified its direct path as before"
  expect_ge "node-e and node-f verified" 2 "$(( $(log_count node-e 'punch_trial: VERIFIED') + $(log_count node-f 'punch_trial: VERIFIED') ))"
  expect_eq "node-e and node-f logged no NO DATA PATH" 0 "$(( $(log_count node-e 'NO DATA PATH') + $(log_count node-f 'NO DATA PATH') ))"

  if [ "$RH_BAD" -gt 0 ]; then
    for n in node-a node-b node-e node-f; do ex "$n" 'mesh-ctl log-since-last-start' > "$H/out/relay-ro-$n.log" 2>/dev/null; done
    fail "$RH_BAD checks missed; sidecar logs: out/relay-ro-<node>.log"
  fi
  phase_end
}

# ---------------------------------------------------------------- RS
# rs_rule : node-b's system answers nothing for a port nobody listens on, as a machine in stealth mode does: it drops the
# TCP resets it would send out of the mesh interface. (Filter table, OUTPUT chain: the sidecar only adds INPUT rules.)
rs_rule() {
  ex node-b 'iptables -I OUTPUT 1 -o potluck0 -p tcp --tcp-flags RST RST -j DROP' \
    || fail "could not install the drop rule on node-b"
  expect_match "node-b has the rule that drops its outbound TCP resets on the mesh interface" 'RST' "$(ex node-b 'iptables -S OUTPUT')"
}

# rs_connect NODE TOIP : the exit status of one TCP connect to a port nobody listens on: 1 refused, 124 no answer within 4 s
rs_connect() {
  ex "$1" "timeout 4 bash -c 'exec 3<>/dev/tcp/$2/9' >/dev/null 2>&1; echo \$?" | tr -d '\r'
}

# rs_once N : one stack start with node-b in stealth mode
rs_once() {
  local it=$1 n bad0=$RH_BAD t0 i=0 va vb
  step "run $it: a fresh stack on the relay; node-b is in stealth mode (it drops its own outbound TCP resets on the mesh interface)"
  RH_AFTER_UP=rs_rule
  rh_stack_up
  RH_AFTER_UP=

  step "run $it: the stealth rule works: a connect to a closed port on node-f is refused at once, on node-b it gets no answer"
  [ "$(rs_connect node-e "$IPF")" = 1 ] && ok "node-e -> node-f port 9: refused (the control pair answers)" || rh_miss "the control pair answers a closed port" "status $(rs_connect node-e "$IPF")"
  [ "$(rs_connect node-a "$IPB")" = 124 ] && ok "node-a -> node-b port 9: no answer (node-b is in stealth mode)" || rh_miss "node-b answers nothing for a closed port" "status $(rs_connect node-a "$IPB")"

  step "run $it: both machines of the stealth pair end VERIFIED, within 30 s"
  t0=$SECONDS
  while [ $((SECONDS-t0)) -lt 30 ]; do
    va=$(log_count node-a 'punch_trial: VERIFIED'); vb=$(log_count node-b 'punch_trial: VERIFIED')
    [ "$va" -ge 1 ] && [ "$vb" -ge 1 ] && break
    sleep 1
  done
  note "node-a verified: $va (by: $(ex node-a "mesh-ctl log-since-last-start | grep 'punch_trial: VERIFIED' | grep -o 'verified_by=[a-z]*' | tr '\n' ' '" | tr -d '\r')), node-b verified: $vb (by: $(ex node-b "mesh-ctl log-since-last-start | grep 'punch_trial: VERIFIED' | grep -o 'verified_by=[a-z]*' | tr '\n' ' '" | tr -d '\r')), after $((SECONDS-t0))s of polling"
  [ "${va:-0}" -ge 1 ] && ok "node-a was verified" || rh_miss "node-a was verified" "not within 30 s"
  [ "${vb:-0}" -ge 1 ] && ok "node-b was verified" || rh_miss "node-b was verified" "not within 30 s"

  step "run $it: neither fell back, neither needed the retry, and both are direct with TCP both ways"
  for n in node-a node-b; do
    [ "$(log_count "$n" 'NO DATA PATH')" -eq 0 ] && ok "$n logged no NO DATA PATH" || rh_miss "$n: NO DATA PATH" "the stealth peer's missing answers ended the trial"
    [ "$(log_count "$n" 'punch_trial: TIMEOUT')" -eq 0 ] && ok "$n logged no trial timeout" || rh_miss "$n: trial timeout" "$(log_count "$n" 'punch_trial: TIMEOUT')"
    [ "$(log_count "$n" 'adding the peer again once')" -eq 0 ] && ok "$n did not add the peer again" || rh_miss "$n: the retry" "the peer was added again"
  done
  sleep 3
  [ "$(api node-a GET /peers | J -r '.peers[0].reachability_mode')" = direct ] && ok "node-a reports node-b as direct" || rh_miss "node-a's mode for node-b" "$(api node-a GET /peers | J -c '.peers[0].reachability_mode')"
  [ "$(api node-b GET /peers | J -r '.peers[0].reachability_mode')" = direct ] && ok "node-b reports node-a as direct" || rh_miss "node-b's mode for node-a" "$(api node-b GET /peers | J -c '.peers[0].reachability_mode')"
  rh_tcp node-a node-b; rh_tcp node-b node-a; rh_tcp node-e node-f; rh_tcp node-f node-e

  if [ "$RH_BAD" -gt "$bad0" ]; then
    for n in node-a node-b node-e node-f; do ex "$n" 'mesh-ctl log-since-last-start' > "$H/out/stealth-run-$it-$n.log" 2>/dev/null; done
    printf 'run %s: MISSED (%s). sidecar logs of this run: out/stealth-run-%s-<node>.log\n' "$it" "$((RH_BAD-bad0))" "$it"
    return 1
  fi
  printf 'run %s: all checks held\n' "$it"
  return 0
}

phase_RS() {
  phase RS "A peer in stealth mode (its system answers no probe): both machines still end verified and direct [sidecar: ${RELAY_LABEL:-working tree}]"
  RH_BAD=0
  local runs=${STEALTH_RUNS:-1} i passed=0 failed_runs=
  for i in $(seq 1 "$runs"); do
    if rs_once "$i"; then passed=$((passed+1)); else failed_runs="$failed_runs $i"; fi
  done
  printf '\nRS RESULT: %s of %s stack starts passed (sidecar: %s)%s\n' "$passed" "$runs" "${RELAY_LABEL:-working tree}" "${failed_runs:+; missed in runs:$failed_runs}"
  SUMMARY="${SUMMARY:-}RS result: $passed of $runs stack starts passed (sidecar: ${RELAY_LABEL:-working tree})
"
  [ "$passed" -eq "$runs" ] || fail "$passed of $runs stack starts passed; missed in runs:$failed_runs"
  phase_end
}
