# Dev tooling

Tooling for people building Potluck itself. If you're a user trying to run
Potluck, you don't need anything in here. See
[`../README.md`](../README.md).

## What's here

### Smoke tests + CI

- `lint_privacy.py`: privacy-invariant linter. CI runs it
  (`.github/workflows/ci-privacy-lint.yml`) on pushes and pull requests
  that touch the files it guards, and `make lint` and the pre-commit hook
  (`make install-hooks`) run it locally. It catches accidental
  member-identity leakage into peer-facing schemas, observability args,
  or the cooperative-routing surface.
- `m3-smoke.py`: end-to-end smoke test for the Phase G m3 (contributor
  network) flow. Needs a running coordinator and at least one peer that
  advertises the requested model. It sends a streaming job and asserts
  the answer was served by a contributor. Run it with `make m3-smoke`.
- `mesh-smoke.py`: smoke test for the mesh control plane against a live
  coordinator and Postgres. It creates a household, issues a pairing
  token, enrolls a peer with a generated WireGuard public key, reads the
  roster, sends a heartbeat and opens the `/mesh/signal` WebSocket. Run it
  with `make mesh-smoke`.
- `mesh-live/`: live check of the mesh sidecar, with real sidecars and WireGuard
  in Docker containers, with assertions. `make mesh-live`. Not part of
  `make test`, not for CI. See its README.
- `test-attestation-handshake.py`: connects to the coordinator's
  `/peers/connect` WebSocket with a fresh Ed25519 key and completes the
  challenge, attest and accept handshake. Its `--invalid-*` flags exercise
  the failure paths.

### Release tooling

- `sign-contributor-release.py`: signs a contributor binary build for
  the cooperative-release allowlist (`contributor_releases` table).
  Maintainers only.

### Contributor-network setup scripts

These scripts join a second machine to a coordinator you run yourself (for
example the one from `make up`) as a contributor peer. They contain no
coordinator address. Set it in the environment before you run them:

| Script | Machine | Environment variable |
|---|---|---|
| `setup-linux-contributor.sh` | Linux box on the same LAN as the coordinator | `POTLUCK_COORDINATOR_HOST`: the coordinator's LAN IP or hostname (required) |
| `setup-linux-tailscale-contributor.sh` | Linux box that reaches the coordinator only over Tailscale | `POTLUCK_COORDINATOR_HOST`: the coordinator's tailnet IP (required) |
| `setup-mac-contributor.sh` | A second Mac | `POTLUCK_COORDINATOR_HOSTS`: space-separated candidates, probed in order (default `127.0.0.1`) |
| `setup-windows-contributor.ps1` | Windows | `$env:POTLUCK_COORDINATOR_HOST`: the coordinator's IP or hostname (required) |

```bash
POTLUCK_COORDINATOR_HOST=<coordinator-ip> ./scripts/dev/setup-linux-contributor.sh
POTLUCK_COORDINATOR_HOSTS="<lan-ip> <tailnet-ip>" ./scripts/dev/setup-mac-contributor.sh
```

```powershell
$env:POTLUCK_COORDINATOR_HOST = "<coordinator-ip>"
.\scripts\dev\setup-windows-contributor.ps1
```

The scripts reach the coordinator on port 8400, probe
`/transparency/fleet`, run `install-contributor.py` and write
`apps/desktop/python/.env` with the coordinator URL, a dev stub member id
and a dev binary hash. A coordinator started with
`REQUIRE_REGISTERED_RELEASE=true` rejects that unregistered hash; sign and
register a build with `sign-contributor-release.py` first. The Linux
scripts also install a systemd user unit. The Mac and Windows scripts add
a launcher for the contributor sidecar.

Related scripts:

- `setup-linux-inference.sh` and `setup-windows-inference.ps1`: add an
  inference backend to a contributor. They pick llama-cpp-python with CUDA
  on NVIDIA and Ollama otherwise. Run them after the contributor setup.
- `start-windows-contributor.cmd`: daily launcher for the Windows
  contributor. It reads `apps\desktop\python\.env`.
- `install-contributor.py`: cross-platform Python installer that the
  setup scripts call. It creates the venv under `apps/desktop/python/` and
  installs the slim contributor requirements.

For the **household contributor** flow (the supported path for adding your
own machines), see [`../setup-household.sh`](../setup-household.sh) instead.

## Conventions

When adding new dev tooling here:

- Bash scripts use the `step / ok / info / warn / fail` helpers: colored
  output, clear failure exits.
- All scripts must be **idempotent**: safe to re-run.
- Values that vary per machine, such as addresses, come from environment
  variables, and the script says so when one is missing.
- Long-running scripts print a summary at the end (PIDs, log paths, next
  steps).
- Failures surface the underlying error message, not a generic "something
  went wrong."
