# Potluck Coordinator

Phase G control plane. Owns the peer registry, attestation handshake, job routing, credit ledger, and the WireGuard mesh control plane (households, mesh nodes, pairing tokens).

Separate FastAPI service from `services/api/` (membership/billing/governance), but **shares the same Postgres**. Each service maintains its own Alembic chain - coordinator uses the `alembic_version_coordinator` version table to avoid colliding with api's `alembic_version`.

## Run locally

Backend stack (Postgres + api + coordinator) via docker-compose from the repo root:

```sh
make up
```

Or run the coordinator natively against a docker-composed Postgres:

```sh
cd services/coordinator
python3.11 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/alembic upgrade head
./.venv/bin/uvicorn coordinator.app:create_app --factory --host 0.0.0.0 --port 8400
```

Health check:

```sh
curl http://localhost:8400/health
```

## Architecture

[`docs/phase-g/mesh-design.md`](../../docs/phase-g/mesh-design.md) describes the mesh control plane: identity and pairing, the signal channel, the roster and the privacy rules. [`docs/security/peer-quality.md`](../../docs/security/peer-quality.md) covers peer quality scoring. The coordinator never puts a member id in a message sent to a peer; `scripts/dev/lint_privacy.py` and the coordinator tests enforce that.

## Bind address

The coordinator binds to `0.0.0.0:8400` so other machines on the LAN can reach it, which cross-machine peer testing needs.

## Tests

```sh
make coordinator-setup   # one time: creates services/coordinator/.venv
make test-coordinator    # runs pytest, including the privacy-invariant tests
```

## Direct circle links

A circle request normally goes through the coordinator (`/jobs/stream` to the lender's `/peers/connect` socket). With direct circle links the coordinator brokers a short-lived link between the requester's machine and a lender's machine in another household; each machine's mesh sidecar adds the other as a restricted WireGuard peer (`circle_peer_add` on `/mesh/signal`) and the request goes machine to machine. The relay stays as the fallback and clients that do not use links are not affected.

Everything is off until switched on, and each setting is read at start (restart the coordinator to change it):

| Env var | Default | Meaning |
|---|---|---|
| `POTLUCK_CIRCLE_LINKS_ENABLED` | off | Kill switch. Off: every link endpoint answers 503 `links_disabled`, attestation never reports `circle_links`, no sweeper, no `circle_peer_*` message is sent. |
| `POTLUCK_CIRCLE_LINK_DIRECT` | off | Transport policy. Off: the two sidecars meet over the DERP relay and neither member learns the other's address. On: public endpoints are shared (LAN candidates only when both report the same public address). |
| `POTLUCK_CIRCLE_LINK_TTL_S` | 600 | Link lifetime, clamped to 5..1800. Renewal is expected at half. |
| `POTLUCK_CIRCLE_LINK_SWEEP_INTERVAL_S` | 15 | How often lapsed links are ended and old rows deleted. |
| `POTLUCK_CIRCLE_LINK_BIND_WITHOUT_SESSION` | off | Development and the harness only. Trust a shared-token engine's `mesh_node_id`. The preflight refuses it in staging and production. |

A machine takes part in links only while its `/mesh/signal` connection was opened with its own node token (`Authorization: Bearer <node_token>`, the token `/mesh/enroll` returns). A connection that names the machine by its WireGuard key alone, which the legacy path still accepts, joins and works as before but never keeps `circle_link_v1`: the machine is not picked as a lender, cannot start a link, and is sent no `circle_peer_*` message. The coordinator logs `mesh_signal_links_need_node_token` for such a connection and `token_bound=<bool>` on every `mesh_signal_connected` line. There is no setting for this. A sidecar that enrolled before node tokens existed has no token and gets no links until it is paired again.

A machine that has a node token has its endpoints written only by something that presented that token: a signal `endpoint_update` on any other connection is dropped (one `mesh_signal_endpoint_needs_node_token` line per connection), and `POST /mesh/heartbeat` with the shared coordinator token or no credential still updates `last_seen_at` but leaves `last_endpoint` alone. A machine enrolled before node tokens existed has no token and is unchanged, and `/mesh/enroll` only creates a row (no endpoint, and a key already enrolled is refused). The direct policy also reads nothing stored: it sends only what the machine's live, token-bound connection has itself reported since it connected, so a value left by an older version or any other path never reaches another household; until a reconnected sidecar has sent its `endpoint_update` (right after every welcome) a link message carries no address for it, and a renewal carries it once known. There is no setting for this.

Known limitation: on the legacy signal path, a hello that names a node by its WireGuard key alone replaces that node's current signal connection (the newer connection wins). A displaced connection does not broadcast `removed` for a node whose newer connection is live.

Enabling links: apply migration `0014_circle_links` (`alembic upgrade head`, or the `coordinator-migrate` service) before switching links on. Restarting the container alone does not create the table. The wire shapes are pinned in `tests/fixtures/circle_link_shapes.json`; the sidecar and the engine are written against that file.
