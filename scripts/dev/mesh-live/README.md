# mesh-live

A live check of the mesh sidecar. It builds `potluck-mesh` and the coordinator
from this working tree, starts three real sidecars (real WireGuard devices,
real routes, real iptables) in containers against a real coordinator and
Postgres, and asserts what they do. It is the check to run after touching the
sidecar's roster, tunnel or routing code, because unit tests cannot show that
data still flows.

It is **not part of `make test` and must not run in CI.** It needs a Docker
daemon that can give containers `/dev/net/tun` and `NET_ADMIN`, builds images,
and takes minutes.

## Run it

```
make mesh-live                      # default phases, about 4 minutes once images are cached
make mesh-live WITH_E2=1 WITH_R=1 WITH_D=1 WITH_FILTER=1   # slow and special phases too
make mesh-live WITH_FILTER=1 PHASES=none FILTER_PHASES="up F"   # only the filter phase F (PHASES=none skips the main pass)
make mesh-live WITH_LINKS=1         # also the direct circle link passes (about 8 minutes per transport policy)
make mesh-live PHASES=none WITH_LINKS=1 LINK_PASSES=relay LINKS_PHASES="L0 L1"   # one link pass, chosen phases
make mesh-live PHASES=none WITH_RELAY=1 RELAY_RUNS=20   # household peers with a relay: 20 fresh stacks, how many passed (about 1.5 minutes each)
make mesh-live PHASES=none WITH_RELAY=1 RELAY_REF=<commit> RELAY_RUNS=20   # the same on a sidecar built from that commit
make mesh-live PHASES=none WITH_RELAY=1 RELAY_PHASES=RS STEALTH_RUNS=10   # a peer in stealth mode, 10 fresh stacks
make mesh-live BEFORE_REF=<commit>   # also run A and C on a binary built from that commit
make mesh-live PHASES="up A W"      # chosen phases (a phase runs what it needs first)
make mesh-live-clean                # only if a run was killed with SIGKILL
```

Exit status is 0 when every assertion held, 1 when one failed (the last line
names the phase, the step and what was expected), 2 when this machine cannot
run it, 130 after Ctrl-C. The stack is torn down on every exit, including
failure and Ctrl-C, so the next run starts clean. The transcript of each run
is `out/mesh-live-<time>.log` (git-ignored).

## What it proves

| Phase | Default | Asserts |
|---|---|---|
| up | yes | the stack starts; the network is internal, publishes no port, and has no route to the internet |
| A | yes | two machines pair, see each other with a fresh handshake, and move TCP both ways with the right source address; `/peers` entries carry `"scope":"household"`; routes and firewall rules are right |
| B | yes | a third machine joins and is removed without restarting the others; routes appear and disappear with it |
| E1 | yes | one sidecar process restarts and the pair recovers |
| C | yes | a roster address outside the mesh (a LAN gateway, loopback, link-local, broadcast, a prefix, IPv6) is refused with a logged reason, only that entry is dropped, the good peer keeps working, and no route to the bad address enters the tunnel |
| W | yes | the same refusal, then the correction, twice, on the live signal channel: traffic both ways after each, no sidecar restarted |
| S | yes | a fresh welcome after a coordinator restart reaches running sidecars and is validated the same way |
| E2 | `WITH_E2=1` | a fast reboot (the coordinator sees the new connection before the old one dies): the pair comes back, helped by the two-minute roster refresh |
| R | `WITH_R=1` | the two-minute roster refresh brings back a peer whose row was bad at start and fixed later (about 4 minutes) |
| D | `WITH_D=1` | an address claimed by this node or by two nodes, and malformed keys, through the real database and through a stand-in coordinator; no key reaches the log |
| Cbefore | `BEFORE_REF` | the same bad address sent to an older binary. By default it must be accepted and routed, which shows phase C can see the problem it guards against. `BEFORE_EXPECT=refuses` flips that |
| F | `WITH_FILTER=1` | the shipped TUN packet filter on a restricted peer (second image, see below). Node-b reaches the one granted TCP port on node-a and no other; a connection from the granted or the peer's own port to another port is blocked; stray ACKs and SYN-ACKs with no tracked flow, fragments, ICMP and UDP never reach node-a's network stack (kernel counters) while the same packets from a household peer do; node-a reaches only the granted port on node-b, and no UDP or ICMP; a household peer is unrestricted both ways; a 30 MB transfer each way matches its sha256; an entry past its deadline cuts an open stream and refuses new connections, and a renewal restores service; removing the entry blocks everything, releasing it returns the peer to the household |
| FO | `WITH_FILTER=1` | fail-open check, about 70 seconds under load: the restricted peer hammers a forbidden port from 16 threads while its entry changes 40 times a second, its sidecar restarts every 7 seconds and node-a's sidecar is killed with SIGKILL once. The forbidden port must accept nothing from anyone |
| FR | `WITH_FILTER=1` | throughput of the restricted peer (flow tracked) against the same peer as a household peer, eight alternating rounds of 400 MB, with min, median, max and spread. Its only assertion on speed is a gross one (restricted median at least a quarter of the household median) |
| FH | `WITH_FILTER=1` | the roster store's address set on a live sidecar: both peers are in it, the host itself and a stranger are not, and when the owner removes a machine at the coordinator its address leaves the set |
| FT | `WITH_FILTER=1` | throughput on the household path with the shipped sidecar against a sidecar built from `BEFORE_REF` (default `HEAD`, which must be a commit without the filter; the phase checks), seven alternating rounds of 400 MB, both sidecars restarted on the binary under test each time. Gross bound only: filtered median at least half of the baseline median |
| RH | `WITH_RELAY=1` | household peers with a relay configured, the case the default run cannot reach (see below). Per stack start: TCP in both directions inside two households straight after both machines of a pair report reachable; then the coordinator restarts and four probe loops (a GET every 200 ms, 1 s limit) run through the new welcome and punch_coord: the longest time without a success in each direction stays under `RELAY_MAX_OUTAGE_MS` (default 5000); then TCP again; every sidecar started a direct-path trial and none timed out |
| RO | `WITH_RELAY=1` | a session that works one way only: node-b drops every inbound WireGuard datagram except handshake messages (raw table, lengths 176 and 120), so handshakes complete and nothing else reaches node-b. Node-a sees node-b's probe packets come out of its tunnel, so it verifies (`verified_by=inbound`) and stays; node-b sees no evidence, adds the peer again once, and ends its trial with `NO DATA PATH` inside the 15 s window. Node-a's traffic to node-b must reach node-b again within `RELAY_RECOVER_S` (default 30) with nothing sent by node-b (node-a -> node-b is polled alone first), and node-b -> node-a after it; the unaffected pair node-e / node-f must verify as usual |
| RS | `WITH_RELAY=1` | a peer in stealth mode: node-b drops its own outbound TCP resets on the mesh interface (an OUTPUT rule on `potluck0`), so a probe to it is never answered (the phase checks that a connect to a closed port on node-b times out where one on node-f is refused). Both node-a and node-b must still end `punch_trial: VERIFIED`, with no `NO DATA PATH`, no timeout and no retry, both `direct`, TCP both ways. `STEALTH_RUNS=N` repeats it on N fresh stacks and prints how many passed |
| L0 | `WITH_LINKS=1` | the link stack (see below) comes up: two households of two machines, a circle with a member in each, a lender's stand-in engine the coordinator binds to the lender's sidecar; every sidecar reports `features: ["circle_link_v1"]` and holds no circle peer, and the coordinator logged `token_bound=True` for the latest signal connection of each (it offers links only on a connection opened with the node's own token, which `/pair` hands the sidecar) |
| L1 | `WITH_LINKS=1` | `POST /me/circles/{id}/links` answers 201 with exactly the fixture's fields; both sidecars list the other in `/circle-peers` (inbound on the lender with `token_sha256` = the hash of the token the requester holds, outbound on the requester) and not in `/peers`; `peers_online_count` counts the household only; the requester's request reaches the lender's gate over the tunnel with the requester's mesh address and a valid token, a wrong or missing token is refused; no line of either log about the link carries the other machine's public address and the token hash is nowhere in a log; under the relay policy no UDP packet leaves either machine for the other |
| L2 | `WITH_LINKS=1` | confinement: the requester cannot reach any other port on the lender, nor send it UDP, ICMP, fragments or stray ACKs (the same packets reach a household partner); the lender cannot open a connection to the requester on any port; the other household's partner machines are not reachable |
| L3 | `WITH_LINKS=1` | household traffic in both households while a link is up: TCP, UDP, ICMP, 40 MB transfers, the filter's drop log, routes and `/peers` |
| L4 | `WITH_LINKS=1` | renew: both sidecars acknowledge and both deadlines move out; renewing again at once is 429 |
| L5 | `WITH_LINKS=1` | `DELETE` removes the peer, the route and the filter entry on both sides, within seconds; idempotent |
| LT | `WITH_LINKS=1` | setup time: POST to the first byte of the lender's answer, `LINK_RUNS` samples each in engine mode (wait for `/circle-peers` to say reachable, then connect, as the requester engine does) and naive mode (connect at once, let TCP retry), with min, median, mean, max and spread |
| L6 | `WITH_LINKS=1` | expiry on the sidecars' own clock: the coordinator container is stopped and both sidecars drop the peer, route and entry at the deadline (not before, not later than 8 s after); household traffic is unaffected |
| L7 | `WITH_LINKS=1` | a coordinator restart (a new welcome on every sidecar, which replaces the device's peer set) keeps the link working with the same token |
| L8 | `WITH_LINKS=1` | the circle owner removes the lender's membership: both sidecars let go within seconds, a renew is 410 `member_removed` |
| L9 | `WITH_LINKS=1` | the lender's sidecar restarts mid-link and the coordinator's replay restores the link |
| LD | `WITH_LINKS=1`, policy `direct+relay` | UDP between the two machines is blocked: the link falls back to the relay after the direct window and carries the request; reported for the engine-style client and for a naive one (which also pays a TCP retransmit timeout, because the peer is created again at the move to the relay and drops what it had queued) |
| LH | `WITH_LINKS=1`, policy `hostile` | a requester rewritten to leave its grant (allowed-ips widened to the lender's household /24, its own packet filter open) sends SYN, ICMP, UDP and ACK packets for the lender's household partner through the circle tunnel while the lender's host forwards IP packets: the lender's filter counts them as wrong_host, the host forwards none, the partner's kernel sees none |
| LU | `WITH_LINKS=1` | `POST /unpair` on the lender's sidecar removes the circle peer, route and entry |

## The packet-filter phases

The filter phases need a peer that is not a household peer, so F, FO, FR and FH run on a second image (`meshlive-sidecar:filter`) built from the same
source plus one harness-only file, `shim/packetfilter_shim.go`. `Dockerfile.sidecar` copies it into `cmd/potluck-mesh/`
of a scratch copy of the source (`SHIM=filter`) and builds with `-tags meshlive_filter`, which excludes the shipped
`packetfilter.go` that defines the same function. The default build has no such file, no tag-gated code in
`internal/tunnel`, and no setting that changes the filter; `go test ./cmd/potluck-mesh` checks that.

The shim does two things to node-a's sidecar. It makes one household address count as "not a household peer", the way a
circle peer would, and it registers that address's entry through the filter's own `SetEntry` and `RemoveEntry`. A small
HTTP server on `/run/filter-shim.sock` in the container lets the phases do that at run time (`/restrict`, `/entry`,
`/release`, `/flap`, `/state`), and `MESHLIVE_FILTER=<ip>:<in>:<out>:<ttl>` in the sidecar's environment does it before
the first packet. Everything the packets go through (wrapper, verdict, flow table, deadlines) is the shipped code.

Facts the phases rely on: Docker containers start with `NET_RAW`, and the compose file lists it anyway; hand-made packets
come from `container/rawsend.py` (raw socket, `IP_HDRINCL`); whether a packet got past the filter is read from the
receiving container's kernel counters in `/proc/net/snmp` (`Tcp OutRsts`, `Ip ReasmReqds`, `Icmp InEchos`,
`Udp NoPorts`), each time next to the same packet from a household peer as a control. `container/acceptlog.py` records
every connection a forbidden port accepts. The clock is the container's, so the sleep and wall-clock-jump cases of the
deadline are covered by unit tests only.

Debugging a failure: `MESH_LIVE_KEEP=1` leaves the stack up on exit (containers `meshlive-node-a-1` and so on), so
you can `docker exec` into it; remove it afterwards with `make mesh-live-clean` and `docker image rm
meshlive-sidecar:filter meshlive-sidecar:after`. `container/connect.py` binds a chosen source port with SO_REUSEADDR,
because `curl --local-port` fails to bind a port that has a connection in TIME_WAIT or a listener, and that failure
looks exactly like a block. A lone later IP fragment cannot be sent from these containers (the sender's netfilter
defragmentation holds it), so only unit tests cover it.

## The household relay phases (RH, RO)

With a relay configured, the coordinator's `punch_coord` starts a direct-path trial on both machines of a pair at the same
moment: DERP off for that peer, the WireGuard peer removed and added again pinned to the direct endpoint. The default run
has no relay, so a `punch_coord` there only updates the endpoint and none of this runs. RH, RO and RS run it on purpose, on the
link stack without the circle parts: postgres, coordinator, STUN, the relay built from `services/derp` (`POTLUCK_DERP_URL=tcp://derp:8443`
on every sidecar), and four sidecars in two households (node-a and node-b; node-e and node-f, seeded as in L0). The network is
the same TEST-NET-2 one as the link passes.

`RELAY_RUNS=N` makes N stack starts one after the other and reports how many passed (`RH RESULT: 17 of 20 stack starts passed`)
instead of stopping at the first miss; the sidecar logs of a missed run are kept as `out/relay-run-<n>-<node>.log`.
`RELAY_REF=<commit>` runs the phase on a sidecar built from that commit with `git archive`, which is how a released sidecar is
measured against the working tree. Phase RO needs the `length` match and the raw table in the Docker VM's kernel. A long RELAY_RUNS
needs the host awake: when a laptop sleeps, Docker's VM is paused, the probe loops' clock jumps and every direction shows the same
multi-minute gap with no failed probe (run under `caffeinate -i -s` on macOS).

What the two defects looked like here: with both machines starting the handshake, the two initiations cross. Each side consumes
the other's before the other's response arrives, the response to its own is rejected, and neither has a session. The
5 s persistent keepalives then repeat the crossing in lockstep, over the relay as well. And when the vendored wireguard-go's
handshake workers interleave on one peer, a side ends with a keypair that is never registered under any index: the other side's
packets are dropped on arrival, one way, while handshakes keep completing on both. The sidecar now lets one machine of the pair
(the lower public key) start the handshake, and a trial counts as verified only when the handshake is fresh and one of two kinds
of evidence has been seen since the peer was last added: a TCP connect from this machine's mesh address to the peer's, through the
tunnel, got an answer (a refusal counts: it is the peer's stack replying), or a packet from the peer's mesh address came out of
the tunnel (a read-only count in the packet filter's `Write`, only for the peers in a trial). The second kind needs nothing from the
peer's operating system, which matters because a machine in stealth mode answers nothing for a closed port. Each machine's probes are
the traffic the other machine's check sees. RS tests that case; RO tests a session that carries nothing inbound. Details are in
`apps/desktop/go/potluck-mesh/cmd/potluck-mesh/trialverify.go`.

## The circle link passes

`WITH_LINKS=1` adds one pass per transport policy (`LINK_PASSES`, default `direct relay direct+relay`). Each pass tears the
stack down and builds it again with `docker-compose.links.yml` layered over the base file; nothing in the default run
reads that file. The pass runs on the default sidecar image, with the shipped packet filter wiring and no shim.

| Policy | Coordinator | Sidecars | What it shows |
|---|---|---|---|
| `direct` | `POTLUCK_CIRCLE_LINK_DIRECT=1` | no relay configured | links over a direct path; the first handshake and the first byte of a request |
| `relay` | `POTLUCK_CIRCLE_LINK_DIRECT=0` (the server default) | `POTLUCK_DERP_URL=tcp://derp:8443`, the relay built from `services/derp` | links over the relay only; neither machine sends the other a UDP packet |
| `direct+relay` | `POTLUCK_CIRCLE_LINK_DIRECT=1` | relay configured | phase LD: direct first, the relay after the window when UDP between the two machines is dropped |
| `hostile` | `POTLUCK_CIRCLE_LINK_DIRECT=1` | no relay; node-a and node-e run other builds (see below) | phase LH |

What the stack stands in for, and how:

- **Two households.** The dev coordinator treats every HTTP caller as one stub member. Household 1 and the circle are made
  through the real endpoints as that member. Household 2 (its `households` row at 100.64.1.0/24, two `pairing_tokens`, and
  the `circle_memberships` row of member 2) is seeded with SQL, and its sidecars enrol through the real `POST /pair`.
- **The lender's engine.** `container/lender_engine.py` attests on the real `/peers/connect` as member 2 with
  `mesh_node_id` (from the lender sidecar's `/status`), `circle_port` and one model for the circle audience, and
  heartbeats; the coordinator runs with `POTLUCK_CIRCLE_LINK_BIND_WITHOUT_SESSION=1`. `container/circlegate.py` is bound to
  the lender's mesh address and applies the engine's gate using only `GET /circle-peers`.
- **The requester's engine** is the harness: curl and `container/linkprobe.py` make the calls the engine makes.
- **Addresses that look public.** The compose file gives the network the TEST-NET-2 subnet 198.51.100.0/24 (still
  `internal`, no route out). The sidecar never sends a circle peer's packets to a private, loopback, link-local or mesh
  address it was given as a public endpoint, and Docker's own subnets are all private. A side effect is that the household
  peers lose the RFC1918 LAN candidates they use in the default run, so the stack's STUN responder answers with the
  WireGuard port (`STUN_REPORT_PORT`), as a port-preserving NAT would show it.
- **A link lasts 60 seconds** (`LINK_TTL_S`), so expiry and renewal happen inside a phase.

The hostile pass (`LINK_PASSES` includes `hostile`) runs phase LH with two other builds, made from the same tree by
`Dockerfile.sidecar` in a scratch copy: node-a is a requester rewritten to leave its grant (`SHIM=hostile`:
`shim/packetfilter_open.go` makes every address count as a household peer, and the circle peer's allowed-ips are widened to
100.64.1.0/24), and node-e is the filter build of the packet-filter phases, which exposes the filter's drop counters, with IP
forwarding switched on. Neither build exists in the default image.

What it cannot show: the LAN candidate path (no two containers share a public address), NAT behaviour of any kind, two
links in opposite directions on one pair of machines (every HTTP caller is the one stub member, so only household 1 can ask),
and macOS or Windows.

## Notes

- Containers are named under the compose project `meshlive`; the script only
  touches that project and the images `meshlive-sidecar:*` and
  `meshlive-coordinator:local`. On exit it removes them. Base images and the
  build cache stay (set `MESH_LIVE_PRUNE_BASE=1` to also remove base images this
  run had to pull).
- The sidecars use a local STUN responder, so nothing goes to a public server.
  Image and module downloads happen at build time only.
- Needs on the host: Docker with `docker compose` v2 and `buildx`, and `git`
  for `BEFORE_REF`. All JSON handling runs in a container, so no `jq` or Python.
- Layout: `run.sh` (phases), `lib.sh` (helpers and assertions), `container/`
  (files copied into the image), `docker-compose.yml`, `Dockerfile.sidecar`,
  `shim/` (the harness-only filter variant, used only by the `filter` image), `links.sh` and `docker-compose.links.yml`
  (the circle link passes), `relay.sh` (the household relay phases RH and RO).
