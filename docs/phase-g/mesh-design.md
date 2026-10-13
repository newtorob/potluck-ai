# Phase G mesh design

**Status:** the coordinator's `/mesh/*` endpoints and signaling WebSocket, the Go sidecar (`apps/desktop/go/potluck-mesh`), pairing, hole-punch coordination and the DERP relay (`services/derp`) are implemented. Items marked **Planned** are design intent that is not built.

**Companion docs:**
- `docs/phase-g/local-ipc.md`: the desktop app to Go sidecar IPC contract (Unix socket or named pipe, HTTP and WebSocket).
- `docs/security/threat-model.md`: adversary model and per-category mitigations for the mesh and the credit ledger.

---

## Goals

1. **No external installs.** A user installs the Potluck app. After first run and pairing, their machines reach each other across networks. No Tailscale account, no terminal, no config files, no separate daemons.
2. **Cross-network connectivity for household machines.** A member's Mac at home reaches their Linux box at the office, regardless of NAT, ISP or geography.
3. **Userspace WireGuard only.** No kernel module and no `wg` CLI on any platform.
4. **Cross-platform.** macOS (arm64 and x86_64), Linux (x86_64 and arm64), Windows (x86_64).
5. **Member-identity-tied.** Every node enrolls to a household owned by a member. No anonymous peers.

## Non-goals

- Cross-household connectivity beyond trusted-circle links. The household mesh spans one member's machines. Direct circle links give a circle member's machine access to one port on a lender's machine (`GET /circle-peers`, see `local-ipc.md`). They are off unless the coordinator's `circle_links_enabled` setting is on.
- iOS and Android clients.
- Mesh-level encryption above the WireGuard layer. WireGuard is enough.
- Multi-region active-active DERP relays. One relay region is deployed.
- An open mesh that anyone can join. Pairing is the only way in.
- Kernel WireGuard support. Userspace only.

## Constraints

- **Userspace WireGuard only** (`golang.zx2c4.com/wireguard`). No `wg` CLI and no kernel module.
- **Two privacy layers.** The mesh layer carries WireGuard-encrypted application traffic and never sees prompt content. Inference-layer blinded routing is enforced at a higher layer, in the coordinator's job dispatch.
  - Mesh layer: household-scoped, member-identified, opaque bytes.
  - Inference layer: cross-household blinded, with `member_id` stripped before a job is sent to a peer.
- **No external installs.** Every dependency is embedded in the app.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Potluck desktop app (Electron + Python sidecar + React)         │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  potluck-mesh sidecar (Go, embeds wireguard-go)            │  │
│  │  • WireGuard userspace tunnel                              │  │
│  │  • STUN client (learns external endpoint)                  │  │
│  │  • Signaling WebSocket to the coordinator                  │  │
│  │  • UDP hole-punch coordination + DERP fallback             │  │
│  │  • Local IPC: "mesh up?", "my IP?", "reachable peers?"     │  │
│  └────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
            │ WSS control                  │ encrypted UDP
            │ (member identity)            │ (opaque bytes)
            ▼                              ▼
┌─────────────────────────────┐   ┌──────────────────────────────┐
│  Coordinator                │   │  Other household nodes        │
│  + mesh control plane       │   │  (direct via hole-punch       │
│  • POST /mesh/enroll        │   │   or via the DERP relay when  │
│  • WS  /mesh/signal         │   │   NAT prevents direct)        │
│  • GET /mesh/peers          │   └──────────────────────────────┘
│  • POST /mesh/heartbeat     │              ▲
└─────────────────────────────┘              │
            │                                │
            ▼                                │
┌─────────────────────────────┐   ┌──────────────────────────────┐
│  Public STUN servers        │   │  DERP relay (services/derp)   │
│  (default Cloudflare and    │   │  TCP/TLS, Potluck framing,    │
│  Google, configurable)      │   │  encrypted bytes only         │
└─────────────────────────────┘   └──────────────────────────────┘
```

The coordinator gains a router for the mesh control plane. The sidecar uses public STUN servers by default (`POTLUCK_STUN_SERVERS` overrides them). The DERP relay is a separate Go service.

---

## What is adopted and what is built here

### Adopted

- **`golang.zx2c4.com/wireguard`** (wireguard-go): the WireGuard userspace library.
- **DERP design from Tailscale.** The relay (`services/derp`) is modeled on Tailscale's DERP. It uses Potluck's own framing, which differs from Tailscale's wire format (see `services/derp/README.md`). Potluck traffic always uses Potluck-operated relays, never relays operated by Tailscale.
- **STUN (RFC 8489)** through the `pion/stun` library.
- **Standard NAT-traversal patterns:** simultaneous-send UDP hole-punch, and a relay fallback for symmetric NAT.
- **`wintun.dll`** on Windows: the prebuilt, signed driver DLL from <https://www.wintun.net/>, bundled under `apps/desktop/go/potluck-mesh/third_party/windows`. Self-built drivers are not redistributed.

### Built here

- **Pairing token system.** Member-identity-tied, household-scoped, single-use, expiring.
- **Coordinator mesh control plane.** Signaling WebSocket, peer roster, mesh-IP allocation, DERP token issue.
- **DERP relay deployment.** Potluck's own relay binary, so user traffic stays on infrastructure the network operator controls.
- **First-run pairing UX.** Electron-native, with a token and status feedback.
- **Cross-platform packaging of the mesh sidecar.** Compiled once per target and embedded in the Electron installer.

---

## Identity and pairing model

The coordinator has `members` (the auth principal) and `peers` (one row per machine attestation). The mesh adds three tables (`services/coordinator/coordinator/models`):

| Table | Columns |
|---|---|
| `households` | `id`, `owner_member_id`, `name`, `mesh_cidr` (unique): a `/24` carved from `100.64.0.0/10` |
| `mesh_nodes` | `id`, `household_id`, `node_name`, `mesh_ip` (unique), `wg_public_key` (unique), `signing_public_key`, `node_token_hash`, `last_endpoint`, `last_seen_at`, `platform` |
| `pairing_tokens` | `token`, `household_id`, `issued_by_member_id`, `expires_at`, `consumed_at`, `consumed_by_node_id` |

A `household_members` table links members to a household. Each node holds two keypairs: a WireGuard keypair, and a separate Ed25519 signing keypair used to prove possession when requesting relay tokens. Neither private key leaves the machine.

### Pairing flow

1. On machine 1, the user clicks **Add another machine** in the desktop app. The app calls `POST /mesh/pairing-tokens` with the member's session. The coordinator returns a single-use token valid for one hour (20 random bytes, base32). The token is returned once. A lost token is replaced by issuing a new one.
2. On machine 2, the user pastes the token (or uses LAN-assisted pairing, described in `local-ipc.md`). The app calls `POST /pair` on the local sidecar.
3. The sidecar generates the WireGuard and signing keypairs if they do not exist.
4. The sidecar calls `POST /mesh/enroll` with `{ pairing_token, wg_public_key, node_name, platform, signing_public_key }`.
5. The coordinator verifies the token (single-use, not expired), allocates the lowest unused address from the household's `/24`, stores the node and marks the token consumed.
6. The coordinator returns `{ node_id, household_id, mesh_ip, mesh_cidr, peers, node_token }`. `node_token` is a per-node credential. The coordinator stores only its hash.
7. The sidecar configures WireGuard, assigns the mesh IP to the TUN interface, opens the signaling WebSocket (`/mesh/signal`) and posts its STUN-discovered external endpoint.
8. The coordinator broadcasts the new node to the household's other online peers as a `roster_update`.

The node name defaults to the machine's hostname plus a 3-hex-character suffix, for example `media-server-7a3`.

---

## Endpoint discovery and connection

### Endpoint discovery

The sidecar queries STUN servers (IPv6 first, then IPv4) to learn its external `ip:port`. It sends the result to the coordinator as an `endpoint_update` and refreshes it every 5 minutes (`POTLUCK_STUN_REFRESH_INTERVAL`). The sidecar also reports private LAN endpoints, up to four per node, which the coordinator accepts only if they are RFC 1918 addresses.

### Path selection

1. **Hole punch.** On `welcome` and on each `roster_update`, the sidecar sends `request_punch` to the coordinator for each peer that has a known endpoint but no fresh WireGuard handshake (older than 3 minutes), at most once per 30 seconds per peer. The coordinator answers with `punch_coord`, and both peers set each other's endpoint at the same moment. WireGuard's handshake retries open the NAT mapping.
2. **DERP relay fallback.** A reachability loop runs every 30 seconds. A peer with no fresh handshake in the last 3 minutes is switched to relay routing through a custom WireGuard bind, and a peer that recovers is switched back.
3. **Direct-path trials.** When a relay is configured, a `punch_coord` starts a trial in which the relay is turned off for that peer and the peer is pinned to the punched endpoint. The trial counts as verified only after a fresh handshake plus either a TCP connect through the tunnel or a packet from the peer's mesh address. Otherwise the peer goes back to the relay. A similar same-LAN trial runs when `POTLUCK_LAN_DIRECT` is set.

**Planned:** an IPv6-direct dial that tries a peer's published IPv6 endpoints before STUN and hole punching. The STUN client already tries IPv6 first, but the sidecar does not publish or dial IPv6 peer endpoints.

### Keepalive and signaling use separate cadences

Confusing these two is the usual cause of battery drain on laptops and cellular connections:

- **WireGuard `persistent-keepalive`.** A UDP packet sent over the tunnel every N seconds to keep stateful NATs from forgetting the mapping. It costs one packet per peer pair, forever. The sidecar leaves it off (0) for ordinary peers and sets a short keepalive only on a peer in a direct-path trial.
- **Application signaling.** The control WebSocket to the coordinator sends one `ping` every 30 seconds. It is one connection, not one per peer. It is how the coordinator knows the node is alive and how the node learns about endpoint changes. The sidecar reconnects with exponential backoff from 500 ms up to 60 seconds.

**Planned:** a slower idle ping interval, an immediate ping on network-transition events, and a user setting for a low-rate WireGuard keepalive so a contributor stays reachable while a laptop sleeps.

### Roster reconciliation (safety net)

The signaling WebSocket is push-based. The coordinator broadcasts `roster_update` deltas only to nodes connected at that moment. A node that was disconnected misses the delta and stays stale until its next full `welcome`.

The Go sidecar therefore runs a roster-refresh loop (`runRosterRefreshLoop`, every 2 minutes). It pulls the authoritative roster with `GET /mesh/peers?wg_public_key=…` and reconciles it against the local peer cache, creating `OnRosterUpdate` calls for any missed additions, removals or endpoint changes. Properties:

- **Diff-based.** A clean cycle emits nothing, so the event bus is not flooded with no-op `peer_updated` events.
- **Pull-only.** The coordinator stays push-only on the WebSocket, and the sidecar pulls.
- **Self-excluded.** The coordinator roster includes the caller, and the loop drops its own `wg_public_key` before diffing.
- **Out of scope:** key rotation (a changed `wg_public_key` for an existing `node_id`) is not reconciled by the loop. It resolves on the next full `welcome`.

The WebSocket stays the real-time path, and this loop backs it up.

### What to expect on different networks

| Network | Expectation |
|---|---|
| Consumer wireline | Direct paths are common. IPv4 is often not behind carrier-grade NAT. |
| Cellular hotspots and home internet over cellular, satellite | Carrier-grade NAT is common, so these peers lean on the relay. |
| Corporate networks | Mixed. Symmetric-NAT firewalls are common, and those peers use the relay. |

### DERP relay outage

There is one relay region today. If it is down, peers on direct paths are unaffected. Peers that depend on the relay lose connectivity until the relay returns or their NAT situation changes and a direct path opens. **Planned:** a second relay region and an in-app notice that the relay is degraded.

---

## Privacy posture

| Component | Sees | Does not see |
|---|---|---|
| Coordinator mesh router | Household roster, member identity, node endpoints | Application traffic, prompt content |
| STUN server | External IP and port of nodes (transient) | Anything else |
| DERP relay | Encrypted bytes, source and destination public keys, byte counts | Plaintext, household membership, member identity |
| Sidecar on a machine | Its own WireGuard tunnel, its own peer roster | Anything outside its household |

The mesh is household-scoped and encrypted. It is not anonymous. Anonymity at the mesh layer would prevent the member attribution that credits and attestation need. The inference-layer blinded-routing invariant is enforced separately at a higher layer: job dispatch strips `member_id` before `JobAssignedMessage`.

The two guarantees live in separate layers, and the code keeps them separate.

**Threat model:** the attacks this design defends against (Sybil, contributor cheating, hostile prompts, mesh penetration, pairing-token theft, coordinator insider, DERP compromise) are listed with their mitigations in `docs/security/threat-model.md`. A new mesh feature should be checked against that doc's categories.

---

## Components

- **Coordinator:** adds the mesh router (`/mesh/*`) and the signaling WebSocket, and applies the mesh migrations.
- **STUN:** public servers by default. Set `POTLUCK_STUN_SERVERS` to use others.
- **DERP:** the Go service in `services/derp`, one region.
- **Desktop sidecar:** `apps/desktop/go/potluck-mesh/`, compiled once per target and embedded in the Electron installer.

---

## Cross-platform engineering

### Target matrix

| Platform | Architectures | Priority | Notes |
|---|---|---|---|
| macOS  | arm64, x86_64 | required | |
| Linux  | x86_64, arm64 | required | |
| Windows | x86_64 | required | Windows 10 21H2 or later (the installer checks this). |
| Windows | arm64 | not built | |
| Linux  | 32-bit ARM | not built | 64-bit Raspberry Pi systems use arm64. |
| iOS and Android | n/a | out of scope | |

### WireGuard userspace per platform

All platforms use `golang.zx2c4.com/wireguard`, which hides the TUN differences. Each platform has a different TUN driver underneath:

- **macOS:** a `utun` device, with no kernel extension. Creating it requires root, so the mesh service runs as root.
- **Linux:** `/dev/net/tun` with `IFF_TUN | IFF_NO_PI`. The process needs `CAP_NET_ADMIN`.
- **Windows:** `wintun.dll`, loaded at runtime. Driver registration happens once, in an elevated install step.

### Background service

A participating machine has to be reachable whenever it is powered on, not only while the desktop app is open. The mesh sidecar therefore runs as a background service that survives the app closing. The templates and privilege model are in `packaging/daemons/README.md`.

| Platform | Mechanism |
|---|---|
| macOS  | A launchd daemon (`ai.trypotluck.mesh.plist`) inside the signed app bundle, registered with `SMAppService`. It runs as root. |
| Linux  | A systemd system unit (`potluck-mesh.service`). The packaged unit uses a dedicated `potluck` user with `CAP_NET_ADMIN`. The app-driven install in `packaging/linux` runs the service as root. |
| Windows | A service installed from an elevated PowerShell script (`packaging/daemons/install-windows-service.ps1`). |

The daemon chowns its local IPC socket to the console user so the desktop app can connect (see `local-ipc.md`). The macOS daemon writes `/Library/Logs/potluck-mesh.log` and `/Library/Logs/potluck-mesh.err`.

### Hostnames

**Planned:** a writer that adds `<node_name>.potluck` entries to the system hosts file (`/etc/hosts`, or `C:\Windows\System32\drivers\etc\hosts` on Windows), which needs admin rights on every platform. It is not built. Machines are addressed by mesh IP, and the desktop UI shows mesh IPs.

A local DNS resolver was considered and rejected. macOS can ignore localhost DNS, the Windows DNS Client service caches aggressively, and Linux needs `nsswitch.conf` changes anyway.

---

## Out-of-scope reminders

- No credits in the mesh layer. The coordinator accounts for credits after job completion, and the mesh does not know about them.
- No mesh-layer ACLs. Every machine in a household reaches every other machine in it. The sidecar's packet filter admits only addresses in the validated household roster.
- No multi-region DERP failover. There is one relay region. If it is down, the mesh degrades to direct paths only.
