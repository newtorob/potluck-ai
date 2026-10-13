# Local IPC: desktop app and Go mesh sidecar

The Go sidecar's network surface (WebSocket to the coordinator, UDP for WireGuard) is described in `mesh-design.md`. This doc covers the other surface: how the desktop app and the local Python sidecar talk to the local `potluck-mesh` sidecar to query state and drive its lifecycle. The server is in `apps/desktop/go/potluck-mesh/internal/localapi`.

---

## Goals

1. The desktop app can ask "is the mesh up?" and "who is in my household?" without a round trip to the coordinator.
2. The desktop app can drive the pairing flow. When the user clicks "Add another machine", the app tells the sidecar to enroll using a pairing token.
3. The desktop app can subscribe to mesh events (peer joined, peer left, endpoint changed, status changed) for live UI updates.
4. One protocol on every OS.
5. Localhost only. The IPC surface is never reachable from another machine.
6. Same user only. Only the user running the desktop app can talk to their own sidecar.

## Non-goals

- Remote management. The sidecar is local-only.
- Multi-tenant use. One sidecar per machine.
- Protocol versioning beyond an `X-Potluck-Protocol-Version` header.
- Authentication beyond filesystem ACLs. The same-user property is enforced by socket-file permissions and the Windows pipe DACL, not by tokens or signatures.

---

## Transport

Unix domain socket on macOS and Linux, named pipe on Windows. Both carry HTTP/1.1 with JSON request and response bodies. The event stream is a WebSocket upgrade.

| Platform | Socket / pipe path |
|---|---|
| macOS  | `/tmp/potluck-mesh.sock` (`XDG_RUNTIME_DIR` is unreliable under launchd) |
| Linux  | `${XDG_RUNTIME_DIR:-/run/user/$UID}/potluck-mesh.sock` by default. The installed system service sets `POTLUCK_LOCAL_API_SOCKET=/run/potluck-mesh.sock`, and the desktop app uses that path first when it exists. |
| Windows | `\\.\pipe\potluck-mesh` |

`POTLUCK_LOCAL_API_SOCKET` (or the `--local-api-socket` flag) overrides the path.

Why HTTP over a local socket rather than a custom framed protocol:
- Mature client libraries exist in Node.js (`http.request({ socketPath })`) and Go (`net.Listen("unix", path)` plus `http.Serve`).
- It is easy to debug: `curl --unix-socket /tmp/potluck-mesh.sock http://localhost/status`.
- The WebSocket upgrade for the event channel is a well-known pattern.
- The traffic never leaves the machine, so on-wire encoding cost is zero.

**Socket-file permissions (Unix):**
- The socket is set to mode `0600` (owner read/write only) immediately after it is created.
- The macOS LaunchDaemon and the Linux service installed by the desktop app run as root. When the sidecar runs as root, it changes the socket's owner to the user it resolves, in this order: the `SUDO_UID` and `SUDO_GID` of a `sudo` launch (a development setup), then the active console or login-session user (`stat /dev/console` on macOS, `loginctl` on Linux). The mode stays `0600`. If no console user can be resolved, the socket stays root-owned and the sidecar logs a warning. Failure to change the owner is never fatal. The owner is resolved again each time the sidecar starts.
- A sidecar that runs as an unprivileged user already owns its socket and does nothing further.

**Windows pipe DACL:**
- Full access goes to two accounts: the account the daemon runs as (LocalSystem when it runs as the boot-time scheduled task) and the one user signed in at the active console, if there is one. Nobody else gets in, and no group is granted.
- The daemon starts at boot before anyone has signed in, so it checks the console user every few seconds and rewrites the pipe's DACL in place when it changes (sign-in, sign-out, fast user switching). The pipe is never closed to do this: connections already open keep working, and there is no moment when the pipe is missing. A client that was refused just before a sign-in succeeds on its next try, a few seconds later.
- Only the physical console session counts. A user connected over Remote Desktop is not the console user and does not get access.

---

## Auth model

The connection is the credential. A process that can open the socket has access. Filesystem permissions (mode `0600` and ownership, or the Windows DACL) are the enforcement mechanism.

The API does not use:
- Bearer tokens. They would be filesystem-readable secrets stored next to the socket, with the same security boundary and more moving parts.
- Per-request signatures. They do not defend against an attacker who already has the socket open.
- Mutual TLS. Traffic is local, and mutual TLS over a Unix socket adds nothing.

Anything that can open the socket has full control over the sidecar: start and stop the tunnel, enroll and unenroll, query state, subscribe to events. The threat model treats the user's machine account as the trust boundary. If that account is compromised, the mesh is compromised regardless of IPC.

---

## Endpoint surface

All endpoints respond with JSON and `Content-Type: application/json`. Errors use an HTTP status code and a body of the form `{ "error": { "code": "...", "message": "..." } }`.

| Endpoint | Purpose |
|---|---|
| `GET /status` | Sidecar state |
| `GET /peers` | Household roster with reachability |
| `GET /circle-peers` | Trusted-circle peers with reachability and grants |
| `POST /pair` | Enroll this machine with a pairing token |
| `POST /unpair` | Leave the household |
| `GET /events` | WebSocket event stream |
| `POST /pairing/broadcast`, `GET /pairing/discovered`, `POST /pairing/reveal` | LAN-assisted pairing |
| `PATCH /settings` | Runtime settings override (coordinator URL) |
| `POST /restart` | Ask the sidecar to restart itself |

### `GET /status`

The sidecar's current state. It is cheap to call. The desktop app polls it every 2 seconds and polls the peer list every 5 seconds.

```json
{
  "version": "<sidecar version>",
  "mesh_status": "running",            // "starting" | "running" | "stopped" | "error"
  "node_id": "uuid-or-null-if-unpaired",
  "household_id": "uuid-or-null-if-unpaired",
  "mesh_ip": "100.64.12.5",             // null if unpaired
  "wg_public_key": "base64-or-null",
  "external_endpoint": "203.0.113.7:51820",  // null until STUN succeeds
  "local_endpoints": ["192.0.2.10:51820"],   // omitted when empty
  "coordinator_connection": "connected",  // "connected" | "connecting" | "disconnected"
  "coordinator_url": "https://coordinator.example",
  "peers_online_count": 3,
  "peers_connecting_count": 0,         // peers flipped to relay routing that have not completed a handshake
  "last_error": null,                  // string when mesh_status == "error", else null
  "vpn_conflict": false,               // a third-party VPN is detected and the coordinator is not connected
  "vpn_detail": "",
  "wg_port_listening": true,           // the WireGuard UDP port is bound locally
  "features": ["circle_link_v1", "restart_v1"],  // optional capabilities; always a list
  "exe_sha256": "3a7b...f1b"           // lowercase hex SHA-256 of the sidecar's own executable, taken at startup; "" if it could not be read; absent in sidecars that predate it
}
```

The desktop app compares `exe_sha256` with the sidecar binary it shipped to tell whether a service that outlived an app update is still the old process.

`features` lists `"restart_v1"` when `POST /restart` (below) is available, and `"circle_link_v1"` when `GET /circle-peers` is available. A sidecar that reports `exe_sha256` but not `restart_v1` restarts itself when its binary is replaced, but cannot be asked to.

### `POST /restart`

Shuts the sidecar down the way SIGTERM does and exits with code 75, so a service manager that restarts on a non-zero exit (launchd `KeepAlive`, systemd `Restart=on-failure`) starts the process again from whatever binary is on disk. The same shutdown runs when the sidecar notices its executable was replaced.

```
POST /restart            (no body)
202 { "restarting": true }
501 { "error": { "code": "restart_unavailable", ... } }   // a build that cannot restart itself
```

The response is written before the shutdown starts. Trust is that of the other commands here: whoever can open the socket. `POTLUCK_MESH_NO_SELF_RESTART` turns off only the binary watcher, not this command. The command exists so the desktop app can restart a root service it cannot signal.

### `GET /peers`

The current household roster from the sidecar's local cache. It mirrors `EnrolledPeer` from `services/coordinator/coordinator/schemas/mesh.py`. The reachability fields are sidecar-local additions:

```json
{
  "peers": [
    {
      "node_id": "uuid",
      "node_name": "media-server-7a3",
      "mesh_ip": "100.64.12.6",
      "wg_public_key": "base64",
      "scope": "household",                // the only value in this list. A reader treats a peer as one of the owner's machines only when this is "household" or absent (older sidecar)
      "platform": "linux",
      "last_endpoint": "203.0.113.9:51820",   // omitted when unknown
      "local_endpoints": ["192.0.2.11:51820"],  // omitted when empty
      "last_seen_at": "2026-01-01T12:00:00Z",
      "reachable": true,                   // the sidecar's view of whether this peer is reachable right now
      "reachability_mode": "direct",       // "direct" | "derp" | "connecting" | null
      "derp_flipped": false,               // this node currently routes to the peer through the relay
      "handshake_age_seconds": 12.4,       // omitted until a handshake has completed
      "key_configured": true               // omitted when unknown (tunnel down); false with reachable false suggests key drift
    }
  ]
}
```

`reachability_mode` is `"connecting"` when this node has switched the peer to relay routing and no WireGuard handshake has completed yet. It is `null` when the node is still trying a direct path.

### `GET /circle-peers`

Peers in trusted circles, with `"scope": "circle"`. An entry has `mesh_ip`, `reachable`, `reachability_mode`, `handshake_age_seconds`, and two optional grants: `inbound` (a peer that may call a port on this host, with a SHA-256 of the bearer token it must present) and `outbound` (a peer whose port this host may call). The response carries no node name, node id, endpoint or WireGuard key, so a UI cannot show a stranger's address. `POST /unpair` clears these peers.

### `POST /pair`

Drives enrollment. Body:

```json
{ "pairing_token": "BASE32-FROM-COORDINATOR", "node_name": "optional" }
```

The sidecar generates a WireGuard keypair if one does not exist on disk, and an Ed25519 signing keypair used for relay token requests. It then calls `/mesh/enroll` on the coordinator with the token, the public keys and the machine's `node_name`. When `node_name` is omitted, the name is `os.Hostname()` plus a 3-hex-character suffix. Success returns 201 with the coordinator's enroll response. A machine that is already enrolled gets 409 `already_enrolled` (call `POST /unpair` first), and a second pairing attempt while one is running gets 409 `pair_in_progress`. Coordinator failures return 502 (`coordinator_error` or `coordinator_unreachable`).

The desktop app drives this from an "Add another machine" wizard:
1. On the first machine, the user clicks "Add another machine". The app calls `POST /mesh/pairing-tokens` on the coordinator and shows the token and a QR code.
2. On the new machine, the user pastes the token or scans the QR code. The app calls `POST /pair` on the local sidecar.

### LAN-assisted pairing

`POST /pairing/broadcast` (body `{ "token": "..." }`, enrolled machines only) announces a pairing invite on the local network for 120 seconds and returns `{ "broadcasting": true, "window_seconds": 120, "confirmation_pin": "..." }`. The announcement carries a one-way fingerprint of the token and never the token itself. The inviter shows the PIN on its own screen.

On a machine that is not enrolled, `GET /pairing/discovered` returns `{ "found": true, "hostname": "...", "fingerprint": "...", "pin_required": true }` for an invite heard within the last 150 seconds, or `{ "found": false }`. `POST /pairing/reveal` (body `{ "fingerprint": "...", "pin": "..." }`) exchanges the PIN a person typed in for the real token, which the inviter holds. A passive listener that only captured the broadcast cannot complete the exchange, and the inviter ends the session after a small number of wrong PINs.

### `POST /unpair`

Leaves the household. It deletes the WireGuard private key, the signing key and its registration marker, and the stored node token, clears enrollment state and any circle peers, and publishes a `status_changed` event with status `stopped`. It returns 204. It does not call the coordinator. To remove the machine from the roster, delete it through the coordinator (`DELETE /mesh/households/current/machines/{node_id}`).

### `PATCH /settings`

Body `{ "coordinator_url": "https://..." }`. The sidecar validates the URL, saves it as a runtime override and reconnects. An omitted field leaves the setting unchanged, and a request without `coordinator_url` returns the current value. An invalid URL returns 422 `invalid_coordinator_url`.

### `GET /events` (WebSocket)

Upgrade from HTTP. The server sends one JSON message per WebSocket frame:

```json
{ "type": "status_changed", "status": "running" }
{ "type": "peer_added",    "peer": { ... } }
{ "type": "peer_removed",  "node_id": "uuid" }
{ "type": "peer_updated",  "peer": { ... } }
{ "type": "endpoint_changed", "external_endpoint": "203.0.113.7:51820" }
{ "type": "error", "code": "...", "message": "..." }
```

The sidecar fans the same stream out to every connected client. Each subscriber has a buffer of 256 events. A client that cannot keep up loses events instead of slowing the sidecar. When the sidecar shuts down it sends a final `status_changed` event with status `stopped`.

---

## Lifecycle

**Who starts the sidecar?** In an installed build it runs as a system service: a launchd daemon on macOS, a systemd unit on Linux, a Windows service. The desktop app installs it, updates it, and shows its state (not installed, requires approval, starting, running, outdated). For development, run `potluck-mesh` from a terminal.

**Discovery:** the desktop app main process calls `GET /status` on the socket path for its platform. A refused connection or a missing socket means the service is not running. The app request timeout is 10 seconds.

**Sidecar exit:** on SIGINT or SIGTERM, the sidecar sends a `status_changed` event to connected WebSocket clients, closes the WireGuard tunnel cleanly and exits 0. When it notices that its own executable was replaced on disk, it shuts down the same way and exits 75, so a service manager that restarts on a non-zero exit starts the new binary (`cmd/potluck-mesh/selfrestart.go`; `POTLUCK_MESH_NO_SELF_RESTART=1` turns this off).

---

## Versioning and forward compatibility

- Every response includes the `X-Potluck-Protocol-Version: 1` header. A client should refuse to interpret responses from a newer major version it does not understand.
- Clients should log and ignore unknown event types, so additive event types do not break old clients.
- Adding endpoints is always safe. Removing them is a major-version change.

---

## Limitations

1. **One socket path per machine.** The path does not include a user name. Two users running Potluck under separate accounts on one machine would share the same path. Set `POTLUCK_LOCAL_API_SOCKET` to a per-user path to separate them.
2. **Windows pipe creation.** The sidecar creates the pipe, so it has to be running before the app can connect. This holds when the sidecar is a Windows service. A client that connects right after the service starts should retry briefly.
3. **Linux console user.** When the system service is root-owned and no console user is logged in (a headless machine), the socket stays root-owned and a desktop app cannot connect until a user signs in and the sidecar restarts.
4. **Key location.** The WireGuard private key (`wg.key`), the signing key (`signing.key`) and the node token sit in one directory. The directory is created with mode `0700` and each file is written with mode `0600`. The default directory depends on the OS and the user running the sidecar, and `/var/lib/potluck/mesh` is used for the Linux system service. Keys are never logged.

---

## Consumers

- The desktop app's Electron main process (`apps/desktop/electron/main.js`) reads `/status` and `/peers`, drives `/pair` and `/unpair`, and checks the service version through `exe_sha256`.
- The local Python sidecar (`apps/desktop/python/potluck_local/services/mesh_ipc.py`) reads `/status` to learn this machine's own mesh address and `/peers` to learn the other machines in the household.
