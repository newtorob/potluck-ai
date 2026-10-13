# Potluck contributor

The code that makes a machine a peer on the Potluck network: it opens a
WebSocket to the coordinator's `/peers/connect`, attests with an Ed25519
device key, heartbeats with live capacity, and serves dispatched jobs.

There are two ways it runs.

## Inside the desktop app (the normal case)

The Contribute compute toggle in the Network view starts an in-process
contributor inside the sidecar (`services/contributor_service.py`). It serves
jobs through the sidecar's own runtime, so the model the app has loaded is the
model this peer advertises. Electron hands the member's session token to the
sidecar (`PUT /contributor/session`), which the contributor presents on the
WebSocket upgrade as `Authorization: Bearer`; the coordinator resolves the
member from it. Turning the toggle off closes the socket, which is what makes
the coordinator drain any job still in flight.

`GET /contributor/status` on the sidecar reports state, this peer's own id,
heartbeat and job counters, and the last error category. The Network view
shows one line from it under the toggle.

## Standalone (dev)

`python -m potluck_local.contributor` runs the same loop with its own engines
(llama-cpp in-process or Ollama over loopback). Settings come from `POTLUCK_*`
environment variables or `.env` (see `config.py`).

```sh
cd apps/desktop/python
export POTLUCK_COORDINATOR_URL=ws://localhost:8400/peers/connect
export POTLUCK_MEMBER_ID=00000000-0000-0000-0000-000000000001   # dev stub
# Optional. Unset, the client uses contributor-release.json next to a frozen
# executable, or "unreleased-dev" from a source checkout. The coordinator
# accepts an unregistered hash unless REQUIRE_REGISTERED_RELEASE=true.
export POTLUCK_BINARY_HASH=59b74f2ea0e8b6f23f3672336f90c97435b1dfa13ef7aabc1994b61c54c7d65a
# Optional. A coordinator that enforces POTLUCK_COORDINATOR_TOKEN needs it on
# the upgrade. Never ship this to end users; the app uses the member session.
export POTLUCK_COORDINATOR_TOKEN=...
# Inference (off = stub-echo peer that advertises no models)
export POTLUCK_INFERENCE_ENABLED=true
export POTLUCK_INFERENCE_BACKEND=ollama          # or llama-cpp with POTLUCK_MODEL_PATH
export POTLUCK_MODEL_SLUG=qwen2.5:3b-instruct-q4_K_M
export POTLUCK_MAX_CONCURRENT_JOBS=1

./.venv/bin/python -m potluck_local.contributor --verbose
```

Expect `contributor_starting`, `connecting`, `session_opened` and then a
`heartbeat_sent` line every 30 s. A `handshake_rejected reason=...` line means
the coordinator turned the machine away; the reason says why.

## What the wire carries

`schemas.py` mirrors `services/coordinator/coordinator/schemas/peer.py`. Keep
the two in sync.

- Attestation: member id, device public key, `binary_hash`, client version,
  platform, hostname, a hardware summary (`system`, `release`, `machine`,
  `cpu_count`, plus `gpu_name`, `gpu_vram_gb`, `recommended_tier`, core
  counts and total RAM when detected), and the models this peer serves with
  measured `tokens_per_second` and `prompt_tokens_per_second` when benchmarked.
- Heartbeat: `current_jobs` (the loop's own in-flight count), `gpu_utilization`
  (0 to 1) and `vram_available_gb` from `telemetry.py`, and
  `max_concurrent_jobs`.
- Jobs: `job_assigned` in, `job_token`, `job_complete` or `job_error` out. A
  job that arrives at the cap is declined on the wire so the coordinator
  reroutes it.

Privacy invariant: prompt content and generated tokens exist only in memory
and in the frames that carry them. Logs carry ids and counters, never text.
Heartbeats carry capacity, never member ids.

## Storage

The per-peer Ed25519 key is stable across restarts:

- macOS: `~/Library/Application Support/Potluck/contributor/peer-key.pem`
- Linux: `~/.local/share/Potluck/contributor/peer-key.pem`
- Windows: `%LOCALAPPDATA%\Potluck\contributor\peer-key.pem`

`POTLUCK_KEY_DIR` overrides the directory. The desktop app registers the
public key with the coordinator through `POST /me/device-keys` when the
toggle turns on; the sidecar's `GET /contributor/device-key` exposes it.

## Build identity

`build.py` writes `contributor-release.json` (SHA-256 over the unsigned
PyInstaller output, the release version, a timestamp) into the bundle. That
hash is what a packaged app presents as `binary_hash`. It identifies the
build inputs; the OS code signature is what protects the shipped bytes.
