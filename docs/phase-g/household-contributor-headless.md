# Household contributor: headless Linux setup

Getting a Linux machine (media server, NAS, dedicated GPU box) running as
a household compute peer, without the desktop GUI. The laptop with the
desktop app drives the request side; the Linux machine just serves.

End result: the laptop's chat scope set to **My machines** streams a prompt
to the Linux box's loaded model over the WireGuard mesh, and the response
streams back. The coordinator is not involved in dispatch.

## Quickstart (recommended)

**Linux** (media server, NAS, headless GPU box):

```bash
git clone https://github.com/newtorob/potluck-ai.git
cd potluck-ai
./scripts/setup-household.sh
```

**Windows**:

```powershell
git clone https://github.com/newtorob/potluck-ai.git
cd potluck-ai
.\scripts\setup-household.ps1
```

The Windows script also builds the Electron desktop app and puts a
"Potluck" shortcut on your Desktop. Double-click it to start the sidecars
and the app, and close it before you run anything else that needs the GPU.

The Linux script auto-detects your distro (Ubuntu/Debian, Fedora/RHEL,
Arch/Manjaro), installs every dependency, builds the mesh sidecar, sets up
the Python venv, walks you through pairing into your laptop's household,
loads a model, and turns on contributor mode. You'll be prompted for:

- Your laptop's coordinator URL (e.g. `http://192.168.1.10:8400`).
- A pairing token (grab it from the laptop: **My Machines → Add another machine**).
- Which model to load (lists installed ones; install one first via
  `curl -X POST http://127.0.0.1:8321/models/<slug>/install` if none).

The script is idempotent: it can be re-run and skips work that is done.
Useful flags:

```bash
./scripts/setup-household.sh --setup-only   # install + build, don't start
./scripts/setup-household.sh --run-only     # skip install, start + pair
./scripts/setup-household.sh --status       # is anything running?
./scripts/setup-household.sh --stop         # stop both sidecars
```

Logs land in `~/.potluck/logs/{mesh,python}.log`.

If your distro isn't auto-detected, or you want to see what each step does,
the rest of this doc is the manual recipe.

## Prerequisites

On the **coordinator-hosting machine** (typically your laptop):

- `make up` is running. The coordinator is bound to `0.0.0.0:8400`
  per `docker-compose.yml`, so other machines on the LAN can reach it.
- Note the LAN IP: `ipconfig getifaddr en0` on Mac, `ip -4 addr show` on
  Linux. Call this `<LAPTOP_LAN_IP>` below.

On the **Linux media server**:

- Go 1.22+, Python 3.11+ available.
- The same Potluck checkout (or a tagged release).
- For WireGuard userspace mode: root, OR `setcap cap_net_admin+ep`
  on the built `potluck-mesh` binary.

## 1. Start the Go mesh sidecar pointing at the laptop coordinator

```bash
cd apps/desktop/go/potluck-mesh
sudo COORDINATOR_URL=http://<LAPTOP_LAN_IP>:8400 \
  DERP_URL=tcp+insecure://<LAPTOP_LAN_IP>:8443 \
  POTLUCK_LOCAL_API_SOCKET=/run/user/$(id -u)/potluck-mesh.sock make run
```

`DERP_URL` points at the relay that `make up` starts on the laptop. Without
it, `make run` expects a relay on the same machine. The socket path is set
explicitly because `sudo` can change `XDG_RUNTIME_DIR`, and step 4 uses this
path.

Leave it running. You'll see `signal: server error on hello: ...
node_not_enrolled` until you pair. That's expected.

## 2. Start the Python sidecar

```bash
cd apps/desktop/python
.venv/bin/python -m potluck_local.app
```

It binds to `0.0.0.0:8321` by default so household peers can reach it over
the mesh. Callers that are not on the same machine can reach only `/peer/*`
and `/health`. Override with `POTLUCK_BIND_HOST=127.0.0.1` if you don't want
it reachable from the mesh.

## 3. Get a pairing token from the laptop

Open the laptop desktop app, go to **My Machines**, and click **Add another
machine**. Copy the token.

## 4. Pair the Linux box via local IPC

```bash
# Socket path as set in step 1. A manually run sidecar without that setting
# uses /run/user/$UID/potluck-mesh.sock (Linux) or /tmp/potluck-mesh.sock (macOS).
curl --unix-socket /run/user/$UID/potluck-mesh.sock \
  -X POST http://localhost/pair \
  -H 'Content-Type: application/json' \
  -d '{"pairing_token": "PASTE-TOKEN-HERE"}'
```

You should see a 201 with the assigned mesh IP (e.g. `100.64.12.6`).
The Go sidecar's signal client stops retrying right away. The IPC endpoints
are described in [local-ipc.md](local-ipc.md).

## 5. Load a model in the Python sidecar

```bash
# List available models
curl http://127.0.0.1:8321/models

# Load one
curl -X POST http://127.0.0.1:8321/models/llama-3.2-3b-instruct-q4/load
```

Pick whatever the hardware can comfortably run. The laptop doesn't care
which slug: household mode probes the peer's `/peer/capabilities` and
uses whatever is loaded.

## 6. Turn on contributor mode

```bash
curl -X PATCH http://127.0.0.1:8321/settings \
  -H 'Content-Type: application/json' \
  -d '{"network": {"contributor_mode": true}}'
```

This is the explicit opt-in to serve inbound household inference. The
`/peer/chat/completions` endpoint returns 503 without it.

## 7. Verify from the laptop

In a terminal on the laptop:

```bash
# Find the Linux box's mesh IP from the My Machines page,
# or query the local sidecar:
curl --unix-socket /tmp/potluck-mesh.sock http://localhost/peers | jq

# Probe its capabilities over the mesh
curl http://<LINUX_MESH_IP>:8321/peer/capabilities
```

The Linux box answers `/peer/*` only for machines its own mesh sidecar lists
as reachable members of this household, and only on its mesh address. The same
request to its Tailscale or LAN address gets a 403, and so does a request made
on the Linux box itself. If the engine on the Linux box cannot read its mesh
socket, it refuses every household request. To check, run
`curl --unix-socket /run/user/$UID/potluck-mesh.sock http://localhost/status`
as the user the engine runs as (use the socket path from step 1).

You should see `available_for_inference: true`. Now open the laptop
desktop app, go to **Chat**, choose **My machines** as the chat scope
(only available when a peer is reachable), type a prompt, and send. The
response streams from the Linux box.

## Running one model across several machines

To combine this machine's memory with another machine's to run a model that
neither can hold alone, see
[Run one model across your own machines](../managed-split-models.md).

## What is not exposed over the mesh

The peer-access middleware in `app.py` restricts non-loopback callers
to `/peer/*` and `/health`. The Linux box's memory, tasks, chat history, and
agent observability are reachable from `127.0.0.1` only, even from other
machines in the same household.

## Tearing down

```bash
# Stop contributor mode
curl -X PATCH http://127.0.0.1:8321/settings \
  -H 'Content-Type: application/json' \
  -d '{"network": {"contributor_mode": false}}'

# Leave the household
curl --unix-socket /run/user/$UID/potluck-mesh.sock \
  -X POST http://localhost/unpair

# Stop the sidecars (Ctrl+C in their respective terminals)
```

The laptop's household roster updates within about 5 seconds (the peer list
refresh interval in `meshStore.ts`).
