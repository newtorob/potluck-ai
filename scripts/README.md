# Potluck scripts

Setup scripts for people, one launcher that picks for you, maintainer
scripts for builds and releases, and a `dev/` folder of tooling for
contributors.

## Just tell me what to run

```bash
./scripts/setup.sh
```

Asks what you're trying to do and runs the right thing. If you'd rather
pick yourself:

| Script | Who it's for | What it does |
|---|---|---|
| `setup-dev.sh` | You want to **develop Potluck itself** | Installs Python venvs, frontend deps, Electron deps, brings up Postgres in Docker, runs migrations. About 3 minutes. |
| `setup-household.sh` | You want a **Linux machine to participate in your household**: a headless server (media box, NAS, GPU rig) or a Linux desktop with a full GUI | Detects your distro, installs Go + Python (+ Node + Electron when a desktop is detected), builds the mesh sidecar (+ Electron app on desktops), pairs you into the household. Drops a "Potluck" entry in your app launcher on a desktop; CLI flow on headless servers. Override the detection with `--gui` / `--headless`. Runs `setup-household-preflight.sh` first. 5-15 minutes the first time. |
| `bootstrap-linux.sh` | You're on a **fresh Ubuntu/Debian box** and `setup-household-preflight.sh` reported that Python 3.11 is missing | One-time OS prep: disconnects Mullvad if connected (a common cause of silent hangs), adds the deadsnakes PPA, installs Python 3.11 + dev headers + git + build tools + wireguard-tools. Optional flags: `--with-tailscale`, `--with-sudo-tickets`. Run with `sudo`. |
| `setup-household-preflight.sh` / `.ps1` | You hit problems and want to **diagnose your environment without running an install** | Seven checks per platform. Linux: VPN/Mullvad, Python 3.11+, sudo, network endpoints, coordinator reachability, disk space, display. Windows: administrator rights, long-path support, disk space, network endpoints, coordinator reachability, execution policy, leftover state from a failed run. Each failure comes with a suggested fix. Pass `--auto-fix` (Linux) or `-AutoFix` (Windows) to apply the safe fixes. Called automatically by `setup-household.sh`. |
| `setup-household.ps1` | You want a **Windows PC to serve inference** when you're not using it | Installs Git/Python/Go/Node/pnpm/WireGuard via winget, builds the mesh sidecar and the Electron desktop app, and adds a "Potluck" shortcut to your Desktop. Pass `-SetupOnly` to skip the shortcut. 10-15 minutes the first time. |
| `setup.sh` | You don't know which of the above you want | Asks you. |

The setup scripts are **idempotent**: safe to re-run as many times as
you want. They skip work that's already done.

## Common flags

For `setup-household.sh`:

```bash
./scripts/setup-household.sh                # full setup + run + pair (the default)
./scripts/setup-household.sh --setup-only   # install + build, don't start anything
./scripts/setup-household.sh --run-only     # skip install, just start + pair
./scripts/setup-household.sh --status       # is anything running? what model? mesh IP?
./scripts/setup-household.sh --stop         # stop both sidecars cleanly
./scripts/setup-household.sh --help         # show this in your terminal
```

## Maintainer scripts

Used when building and releasing Potluck or when checking a deployment.
Contributors rarely need them.

| Script | What it does |
|---|---|
| `build-windows-installer.ps1` | Builds the Windows installer from a clean checkout. Runs the bundle steps explicitly (the Makefile chain assumes a POSIX shell) and fails if the GPU backend set is missing from the bundle. Flags: `-SkipDeps`, `-AllowDirty`. |
| `build-split-engine.py` | Builds the pinned household inference engine from a llama.cpp source checkout (`--source`, `--backend metal\|cuda\|cpu`). Output defaults to `apps/desktop/python/native/split`. |
| `release_upload.sh` | Uploads a finished desktop build from `apps/desktop/electron/out/` to the release bucket with `rclone`: installers first, update feed second. Supports `--dry-run`. |
| `check_model_updates.py` | Compares the shipped model catalog (`apps/desktop/python/potluck_local/data/models.json`) with a curated lineup using public Hugging Face metadata. `--json` for machine output, `--write` to emit a proposed catalog. Run it with `make check-models`. |
| `smoke_prod.sh` | Read-only smoke test of a deployed API and coordinator. Run it with `make smoke-prod`; `API_URL` and `COORDINATOR_URL` override the targets. |
| `deploy/gen-fly-secrets.sh` | Generates random secrets for a Fly.io deployment and prints the matching `fly secrets set` commands. Values print once to the terminal. |
| `git-hooks/pre-commit` | The pre-commit hook that runs the privacy lint. Install it with `make install-hooks`. |
| `tests/` | pytest tests for the privacy lint, the release-hash SQL stamp and the service readiness probes. `test_service_readiness.py` documents the `PYTHONPATH` it needs in its docstring. |

## What's in `dev/`?

Tooling for contributors: smoke tests, the live mesh check, release
signing, the privacy lint that CI runs, and the contributor-network setup
scripts that read the coordinator address from the environment. If you're
not contributing code to Potluck, you can ignore that folder.

See [`dev/README.md`](./dev/README.md) for the breakdown.

## I'm on Mac / Windows

- **Windows**: run `setup-household.ps1`. It installs the full Electron app
  so you get a normal GUI on the PC. Open PowerShell in the repo root and
  run `.\scripts\setup-household.ps1`. The app is built locally on your
  machine.
- **Mac**: there is no household setup script. Run the desktop app (the
  installer, or `make desktop` from the repo root) and add other machines
  from its Household view.

For setting up another machine via the GUI (rather than scripting), open
the desktop app on any device and use the **Household** view's "Add another
machine" button to generate a pairing token.
