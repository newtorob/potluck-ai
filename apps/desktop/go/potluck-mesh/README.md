# potluck-mesh

Potluck AI mesh sidecar. Embeds a userspace WireGuard tunnel (wg-go) and
maintains a persistent signaling WebSocket to the Potluck coordinator.

Architecture context: `docs/phase-g/mesh-design.md`.

## What this is

A background process that runs on each Potluck member machine. It:

1. Generates a WireGuard keypair on first run (private key stays on-device)
2. Connects to the coordinator's `/mesh/signal` WebSocket
3. Sends `hello` with its WG public key; receives `welcome` with the household roster
4. Assigns the mesh IP to the local TUN interface
5. Configures WG peers from the welcome roster
6. Performs STUN to discover the external UDP endpoint; sends `endpoint_update`
7. Refreshes STUN every 5 minutes; sends a new `endpoint_update` if the endpoint changed
8. On `roster_update`, applies peer additions, endpoint changes, and removals incrementally
9. Sends `request_punch` to the coordinator for peers with known endpoints that don't have a fresh WG handshake
10. On `punch_coord`, without a relay it only calls `UpdatePeerEndpoint`. With a relay configured it starts a direct-path trial: relay off for that peer, WG peer removed and added again pinned to the punched endpoint. The machine with the lower public key starts the handshake at once; the other waits half a second and starts its own only if none has arrived (two initiations in flight at once leave wireguard-go with no session or a one-way one). A trial counts as verified only when the handshake is fresh and either a TCP connect through the tunnel to the peer's mesh address gets an answer or a packet from the peer's mesh address comes out of the tunnel (the packet filter counts those for the peers in a trial); otherwise the peer is added again once and then the trial goes back to the relay (`cmd/potluck-mesh/trialverify.go`)
11. Sends a `ping` every 30 seconds and logs `pong` responses
12. Reconnects with exponential backoff on any disconnect

## Hole-punch coordination

- UDP hole-punch coordination via `request_punch` / `punch_coord` signal messages
- `OnPunchCoord` handler: without a relay, both initiator and responder call `UpdatePeerEndpoint` simultaneously, letting WG's handshake-init retries punch through cone NATs
- Punch throttle: at most one `request_punch` per peer per 30 seconds; skipped entirely if the peer already has a fresh handshake (<3 minutes old)
- `Tunnel.PeerReachability`: queries wg-go's UAPI for `last_handshake_time_sec`, `rx_bytes`, `tx_bytes` per peer
- Reachability log goroutine: logs state transitions (unreachable→reachable, reachable→unreachable) every 30s; startup state logged once per peer
- Windows mesh IP assignment via `netsh interface ip add address` (idempotent)

## Limitations

- The sidecar does not write `/etc/hosts`, so `<hostname>.potluck` names do not resolve. Peers are addressed by mesh IP.
- `POST /unpair` wipes the local keys and enrollment state. It does not call the coordinator. The coordinator exposes `DELETE /mesh/nodes/{node_id}` to remove a node from its household.
- The default IPC socket path on macOS is `/tmp/potluck-mesh.sock`, one path per machine. Two users running the sidecar on the same Mac would use the same path.

## Build

Requires Go 1.22+.

```bash
cd apps/desktop/go/potluck-mesh

# Build for the current platform into dist/
make build

# Build for all 5 target triples (darwin/arm64, darwin/amd64, linux/amd64,
# linux/arm64, windows/amd64); output in dist/
make build-all

# Tests and vet
go test ./...
make lint
```

`make build` on Windows, and `make build-all`, copy `third_party/windows/wintun.dll`
into `dist/`. `wintun.dll` is loaded at runtime when the TUN device is created
(`tun.CreateTUN()`); it is not linked at compile time, so ship it next to
`potluck-mesh.exe`. `make build-all` builds the Windows binary with
`CGO_ENABLED=0`. See `third_party/windows/README.md`.

From the repo root, `make sidecar-go` builds the sidecar for the current
platform and `make test-go` runs the Go tests for the sidecar and the DERP relay.

## Run against a local coordinator

```bash
# Start the coordinator (in another terminal):
make coordinator   # from repo root

# Start the sidecar. On first run it generates a keypair and waits to be paired:
./dist/potluck-mesh --coordinator-url http://127.0.0.1:8400

# Create a household and a pairing token on the coordinator:
curl -s -X POST http://127.0.0.1:8400/mesh/households \
  -H 'Content-Type: application/json' -d '{"name":"dev"}' | jq .
curl -s -X POST http://127.0.0.1:8400/mesh/pairing-tokens | jq .

# Enroll the sidecar through its local API (see "Local IPC" below):
curl --unix-socket /tmp/potluck-mesh.sock \
  -X POST http://localhost/pair \
  -H 'Content-Type: application/json' \
  -d '{"pairing_token":"<TOKEN>","node_name":"dev-mac"}' | jq .
```

Subsequent runs connect directly with the keys and enrollment state saved on disk.

## DERP relay

When direct UDP hole-punch fails (symmetric NAT, restrictive firewall), the
sidecar routes WireGuard packets through a DERP relay (see `services/derp`).

### How it works

1. Set `POTLUCK_DERP_URL` (or `--derp-url`) to a running relay. Supported schemes:
   `tcp+tls://<relay-host>:443` for a TLS relay, `tcp+insecure://127.0.0.1:8443`
   (alias `tcp://`) for a local dev relay. `make run` uses the insecure local
   relay by default.
2. On startup, `DerpBind` wraps the inner `conn.StdNetBind` and is installed as
   the WireGuard device's bind interface.
3. With a relay configured, peers start on the relay: `DerpBind.SetPeerRouting(pubkey, true)`
   marks the peer as relay-routed and `tunnel.UpdatePeerEndpoint(pubkey, "derp:<hex-pubkey>")`
   gives WG a synthetic endpoint, so outbound packets flow through `DerpBind.Send`
   to the relay. The relay is the floor: a peer only leaves it after a direct
   path has been verified.
4. Inbound: the DERP client's `onPacketRx` callback calls `DerpBind.Inject`,
   which queues the packet for delivery via the DERP `ReceiveFunc` that wg-go is
   blocking on. WG decrypts the packet and delivers it to the TUN interface as
   normal.
5. A coordinator `punch_coord` starts a direct-path trial (see item 10 above).
   A verified trial moves the peer to the punched endpoint; a trial that times
   out, and a direct path whose handshake goes stale (older than 3 minutes), put
   the peer back on the relay and delay the next trial.
6. A trial for peers on the same LAN (using the LAN candidates the roster
   carries) is off by default. Setting `POTLUCK_LAN_DIRECT` to any non-empty value
   at start turns it on; the value is read once at process start.

### Synthetic endpoint format

Synthetic endpoints use the string format `derp:<64-hex-chars>` where the hex
string is the full 32-byte WireGuard public key. Example:

```
derp:ab12cd34ef56789012345678901234567890123456789012345678901234abcd
```

`DerpBind.ParseEndpoint` detects this prefix and returns a typed
`syntheticEndpoint` value; any other string is delegated to the inner
`StdNetBind.ParseEndpoint`.

### Debugging relay routing

Use `--log-format text`. Key log messages:

```
# Peer moved onto the relay:
derp_fallback: peer flipped to DERP routing   pubkey_prefix=ab12cd34...  synthetic_ep=derp:ab12cd34...

# Inbound relay packet delivered to WG:
derp_bind: delivered DERP packet to WG device   src_prefix=ab12cd34...   payload_len=148

# Outbound to the relay failed (success is silent):
derp_bind: relay send failed
```

To test relay routing, start both sidecars with a relay configured and disable
UDP between them (for example with a local firewall rule).

---

## Two-sidecar cross-network test

Two nodes on **different networks** (for example one on home Wi-Fi, one
tethered to a phone hotspot) should establish a WG handshake via hole-punch
within about 5-10 seconds, without any LAN adjacency.

**NAT coverage:** works with full-cone, address-restricted, and
port-restricted cone NATs. Symmetric NAT (carrier-grade NAT with port
randomisation per destination) still fails to hole-punch; with a DERP relay
configured the peers stay on the relay.

### Prerequisites

- Two machines enrolled in the same household (see the enrollment steps above)
- The coordinator reachable from both (public URL or a tunnelled dev instance)
- Both machines on separate NAT-ted networks (different public IPs)
- `ping` available on both machines

### Step 1: enroll both nodes (if not already done)

Follow the normal enrollment flow on each machine. Use a coordinator with a
publicly routable address or expose your local dev coordinator via a tool like
`ngrok`/`cloudflared`:

```bash
# Expose local coordinator over HTTPS (example using cloudflared):
cloudflared tunnel --url http://127.0.0.1:8400

# On each machine, set POTLUCK_COORDINATOR_URL to the tunnel URL:
export POTLUCK_COORDINATOR_URL=https://<tunnel-hostname>
```

### Step 2: start both sidecars

On each machine start potluck-mesh pointing at the shared coordinator:

```bash
# Machine A (home Wi-Fi):
./dist/potluck-mesh --coordinator-url https://<coordinator-host>

# Machine B (phone hotspot or different ISP):
./dist/potluck-mesh --coordinator-url https://<coordinator-host>
```

### Step 3: watch the hole-punch logs

Within a few seconds of both machines going online you should see on **each** sidecar:

```
# STUN discovers the external endpoint:
INFO stun: initial endpoint discovered    endpoint=203.0.113.42:51820

# The sidecar asks the coordinator to coordinate a punch:
INFO punch: sending request_punch         node_id=<uuid> pubkey_prefix=XXXXXXXX...

# Coordinator notifies each side via punch_coord:
INFO punch_coord received                 peer_node_id=<uuid> role=initiator endpoint=198.51.100.7:51820
INFO tunnel: peer endpoint updated        pubkey_prefix=XXXXXXXX... endpoint=198.51.100.7:51820

# wg-go fires handshake-init toward the peer (visible at DEBUG level):
# [wg] Sending handshake initiation to peer X (198.51.100.7:51820)
# [wg] Received handshake response from peer X

# Reachability loop confirms the session is live (within 30s):
INFO reachability: initial state          node_id=<uuid> reachable=true last_handshake_ago=2s
```

### Step 4: ping across the mesh

Once both sidecars log `reachable=true`:

```bash
# From machine A, ping machine B's mesh IP:
ping 100.64.0.2

# From machine B, ping machine A's mesh IP:
ping 100.64.0.1
```

Expected: round-trip latency equal to the actual network path between the two
machines (typically 5-50ms for same-country residential connections). The first
ping may take up to ~200ms while WG completes the handshake.

### Troubleshooting

- **No `punch_coord` arrives:** the coordinator's `request_punch` handler
  requires both peers to have reported their STUN endpoints. Wait for both
  `stun: initial endpoint discovered` lines before concluding it's broken.
- **`punch_coord` arrives but handshake never completes:** likely symmetric NAT
  on one or both sides. The source port that STUN observed is not the same port
  WireGuard uses toward the peer. A DERP relay handles this case.
- **`reachable=false` after punch_coord:** the handshake-init packets may have
  been dropped by a firewall. Check that UDP egress is not blocked on either
  machine. Some corporate VPNs and hotel Wi-Fi networks block arbitrary UDP.
- **Windows: `netsh add address` fails with access denied:** the Wintun adapter
  was created by a different user account than the one running potluck-mesh.
  Run the sidecar as the same user that installed Potluck (or as Administrator).

---

## Two-sidecar LAN smoke test

After both sidecars are running, you should be able to ping one node's mesh IP
from the other.

**Prerequisites:** two machines (or two separate user accounts on the same
machine, each with their own key path) that can both reach the coordinator.

### Step 1: enroll both nodes

On each machine, do the enroll flow above. Both nodes end up in the same
household. The coordinator allocates consecutive mesh IPs from the household
`/24` (e.g. `100.64.0.1` and `100.64.0.2`).

### Step 2: start both sidecars

```bash
# Machine A:
./dist/potluck-mesh --coordinator-url http://<coordinator-ip>:8400

# Machine B (separate terminal / machine):
./dist/potluck-mesh --coordinator-url http://<coordinator-ip>:8400 \
  --wg-private-key-path /tmp/node-b-wg.key
```

Watch the logs. You should see on each sidecar:

```
INFO stun: initial endpoint discovered endpoint=<public-ip>:<port>
INFO signal: connected node_id=... mesh_ip=100.64.0.1 roster_size=1
INFO tunnel: mesh IP assigned to interface interface=potluck0 mesh_ip=100.64.0.1
INFO tunnel: peers configured count=1
INFO signal: roster updated ...   # when the other sidecar sends endpoint_update
INFO tunnel: peer endpoint updated pubkey_prefix=XXXXXXXX endpoint=<ip>:<port>
```

### Step 3: ping across the mesh

On macOS/Linux, once both sidecars have logged "peer endpoint updated":

```bash
# From machine A, ping machine B's mesh IP:
ping 100.64.0.2

# From machine B, ping machine A's mesh IP:
ping 100.64.0.1
```

The first ping packet triggers a WireGuard handshake (visible in DEBUG logs as
`[wg] Sending handshake initiation`). Once the handshake completes, latency
drops to wire speed. On a LAN this is typically < 1 ms.

On a local network both nodes' STUN-reported endpoints are reachable directly,
so no NAT traversal is needed.

### Troubleshooting

- **ping: sendmsg: no route to host:** the TUN interface address is not assigned.
  On Linux, check `ip addr show potluck0`. Ensure the sidecar has `CAP_NET_ADMIN`
  (`sudo setcap cap_net_admin+ep ./dist/potluck-mesh`).
- **Handshake never completes:** the peer's endpoint may be a private IP behind
  NAT. Check both sidecars' STUN-reported endpoints. If they're RFC-1918 addresses
  STUN reached the router, not the internet. Use the LAN IP directly, or use
  the cross-network test above.
- **`netsh add address` error on Windows:** ensure Wintun is installed and the
  sidecar runs as the same user that created the adapter. See the Windows
  troubleshooting note in the cross-network test section above.

## Local IPC

The sidecar exposes a local HTTP+WebSocket server over a Unix socket (macOS/Linux)
or named pipe (Windows). Electron connects to this surface to query mesh state and
drive the pairing flow. No network access required. The socket is owner-read/write
only (mode 0600 on Unix; user-DACL on Windows).

### Socket paths

| Platform | Default path |
|---|---|
| macOS | `/tmp/potluck-mesh.sock` |
| Linux | `${XDG_RUNTIME_DIR:-/run/user/$UID}/potluck-mesh.sock` |
| Windows | `\\.\pipe\potluck-mesh` |

Override with `--local-api-socket` / `POTLUCK_LOCAL_API_SOCKET`. The installed
Linux service pins `/run/potluck-mesh.sock`; see `packaging/linux/README.md`.

### Endpoints

All responses include `X-Potluck-Protocol-Version: 1`.

```
GET   /status              current mesh state (mesh_status, mesh_ip, peers_online_count, ...)
GET   /peers               household roster with per-peer reachability (direct/derp/null)
GET   /circle-peers        peers of trusted circles that this node lends to or borrows from
POST  /pair                enroll this node using a pairing token from the coordinator
POST  /unpair              wipe local WG keys and enrollment state and stop the mesh
POST  /pairing/broadcast   an enrolled machine announces a pairing invite on the LAN; returns a confirmation PIN
GET   /pairing/discovered  an unenrolled machine asks for an invite heard on the LAN (a fingerprint, not a token)
POST  /pairing/reveal      exchange the fingerprint and the PIN read off the inviter's screen for the pairing token
PATCH /settings            runtime settings override (currently: coordinator_url)
POST  /restart             shut down gracefully and exit 75 so the service manager starts the process again
WS    /events              newline-delimited JSON event stream (status_changed, peer_added, ...)
```

### Quick smoke test

With `potluck-mesh` running and the coordinator up:

```bash
# 1. Check status (should show mesh_status: "starting" or "running"):
curl --unix-socket /tmp/potluck-mesh.sock http://localhost/status | jq .

# 2. Create a household + issue a pairing token:
curl -s -X POST http://127.0.0.1:8400/mesh/households \
  -H 'Content-Type: application/json' -d '{"name":"dev"}' | jq .
TOKEN=$(curl -s -X POST http://127.0.0.1:8400/mesh/pairing-tokens | jq -r .token)

# 3. Drive enrollment via local IPC:
curl --unix-socket /tmp/potluck-mesh.sock \
  -X POST http://localhost/pair \
  -H 'Content-Type: application/json' \
  -d "{\"pairing_token\":\"$TOKEN\",\"node_name\":\"smoke-laptop\"}" | jq .

# 4. Confirm status shows mesh_ip:
curl --unix-socket /tmp/potluck-mesh.sock http://localhost/status | jq .mesh_ip

# 5. Subscribe to events (use websocat or any WS-over-unix-socket client):
# websocat unix:/tmp/potluck-mesh.sock -E 'GET /events HTTP/1.1'
```

## Configuration

Flags and environment variables (flags take precedence):

| Flag | Env var | Default | Description |
|---|---|---|---|
| `--coordinator-url` | `POTLUCK_COORDINATOR_URL` | `http://127.0.0.1:8400` | Coordinator base URL (`http://` or `https://`) |
| `--coordinator-token` | `POTLUCK_COORDINATOR_TOKEN` | *(empty)* | Shared secret presented to the coordinator; empty means dev mode |
| `--wg-private-key-path` | `POTLUCK_WG_PRIVATE_KEY_PATH` | platform-specific (see below) | WG private key file path |
| `--wg-public-key` | `POTLUCK_WG_PUBLIC_KEY` | derived from private key | WG public key (optional verification) |
| `--signing-key-path` | `POTLUCK_SIGNING_KEY_PATH` | `signing.key` next to the WG key | Ed25519 key used to prove key possession when requesting a DERP token |
| `--node-name` | `POTLUCK_NODE_NAME` | `$HOSTNAME` | Human-readable node name |
| `--log-format` | `POTLUCK_LOG_FORMAT` | `text` | `text` (dev) or `json` (production) |
| `--stun-servers` | `POTLUCK_STUN_SERVERS` | cloudflare + google (see below) | Comma-separated STUN server list `host:port` |
| `--stun-refresh-interval` | `POTLUCK_STUN_REFRESH_INTERVAL` | `5m` | How often to re-query STUN (Go duration string) |
| `--derp-url` | `POTLUCK_DERP_URL` | *(empty, DERP disabled)* | DERP relay URL; see the DERP relay section |
| `--local-api-socket` | `POTLUCK_LOCAL_API_SOCKET` | platform default (see Local IPC) | Unix socket or pipe path for Electron IPC |
| - | `POTLUCK_LAN_DIRECT` | *(unset, off)* | Any non-empty value turns on the same-LAN direct-path trial |
| - | `POTLUCK_MESH_NO_SELF_RESTART` | *(unset)* | Set to `1` to turn off the watcher that restarts the sidecar when its executable is replaced |

Default STUN servers (tried in order, IPv6-first per attempt):
1. `stun.cloudflare.com:3478`
2. `stun.l.google.com:19302`

Default key paths:
- macOS: `$HOME/Library/Application Support/Potluck/mesh/wg.key`
- Linux: `$HOME/.local/state/potluck/mesh/wg.key`
- Windows: `%LOCALAPPDATA%\Potluck\mesh\wg.key`

When running as a system daemon (the production deployment mode), `$HOME` is
not set and the key falls back to `/var/lib/potluck/mesh/wg.key` (Linux/macOS)
or `C:\ProgramData\Potluck\mesh\wg.key` (Windows).

## Platform notes

**macOS:** Creating the `utun` device requires root. The installed app runs the
sidecar as a root LaunchDaemon (see `packaging/daemons/README.md`); in
development, run it with `sudo`. The kernel allocates the `utun` index and the
sidecar asks for `utun9`. Mesh IP assignment uses `ifconfig`.

**Linux:** Requires `CAP_NET_ADMIN` to open `/dev/net/tun`. The systemd units
grant it with `AmbientCapabilities=`. In dev, either run as root or set the cap:

```bash
sudo setcap cap_net_admin+ep ./dist/potluck-mesh-linux-amd64
```

Mesh IP assignment uses `ip addr add`.

**Windows:** Requires the Wintun driver installed once (admin UAC prompt during
the Potluck installer). After that, no elevation needed at runtime.
Mesh IP assignment uses `netsh interface ip add address`. `netsh` is present on
all supported Windows versions and does not require elevation after the Wintun
adapter exists in the system.

## Privacy

The sidecar handles WireGuard-encrypted packets only. It never sees prompt
or response content. Logging is restricted to mesh-layer metadata:
- Node IDs and mesh IPs in roster events
- External endpoints (ip:port strings), which are already public mesh-layer metadata
- WG public key prefixes (first 8 chars) for peer identification in logs
- WG tunnel control-plane events at Debug level
- No logging of WG private keys, pairing tokens, STUN raw responses, or inference content

The coordinator's privacy invariant (no member id in the job-assignment message sent to a
peer) is enforced separately in `services/coordinator/coordinator/routers/jobs.py`.
