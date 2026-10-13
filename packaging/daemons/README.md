# System-daemon install templates

Config for running the mesh sidecar (`potluck-mesh`) as a system service that survives the user logging out. Without it, a machine can only contribute compute while someone is logged in and using it.

macOS and Linux/Windows use different install models:

- **macOS** ships a **static** plist embedded in the signed `.app` at build time and registers it via `SMAppService`. It is NOT rendered by an installer and has no substitution tokens (SMAppService rejects a plist that changes after signing). The coordinator URL is baked in; runtime overrides go through `PATCH /settings` on the sidecar socket.
- **Linux/Windows** still render the templates below into real config files at install time.

## Files

| Platform | Template | Installed to | Service user |
|---|---|---|---|
| macOS  | `ai.trypotluck.mesh.plist` (static) | `Contents/Library/LaunchDaemons/` in the app bundle, registered via `SMAppService` | `root` (like `tailscaled`) |
| Linux  | `potluck-mesh.service`     | `/etc/systemd/system/potluck-mesh.service`        | `potluck` (created by `.deb`/`.rpm` postinst) |
| Windows | `install-windows-service.ps1` | run from the app via elevated PowerShell; service name `PotluckMesh` | `NT SERVICE\PotluckMesh` (virtual account) |

## Substitution tokens

The Linux and Windows templates contain installer-time tokens that get replaced when the installer renders them onto disk (the macOS plist is static, see above):

- **`{{BINARY_PATH}}` / `@BINARY_PATH@`**: absolute path to the installed `potluck-mesh` binary on the target platform.
- **`{{COORDINATOR_URL}}` / `@COORDINATOR_URL@`**: bootstrap coordinator URL. The user can override via the desktop app's Settings → Advanced → Cooperative connection. The daemon picks up overrides via the IPC contract (the desktop app passes its preferred URL through).
- **`{{KEY_DIR}}` / `@KEY_DIR@`**: directory for the WG private key + identity material. `0700`, owned by the service user.
- **`{{LOG_DIR}}` / `@LOG_DIR@`**: daemon log directory. `0755`, owned by the service user (write) + readable by admins.

## Privilege model

The Linux service uses `AmbientCapabilities=CAP_NET_ADMIN` (set by systemd) **plus** `setcap cap_net_admin+ep` on the binary itself (set by the `.deb`/`.rpm` postinst). Belt-and-suspenders, either alone is sufficient, both together survive a manual binary swap.

The macOS service runs as **root**. Creating a `utun` device on macOS requires root unless the process carries the Network Extension entitlement (a paid, App-Review-gated path we deliberately avoid), so we run the daemon as root the same way Tailscale's non-App-Store macOS client runs `tailscaled`. The daemon then chowns its Unix socket to the console user so the user-level app can reach it (see `internal/localapi/listener_unix.go`).

The Windows service runs as a virtual `NT SERVICE\PotluckMesh` account. wintun driver registration happens during the elevated install step (UAC prompt).

## Privilege footprint

The mesh sidecar handles WG-encrypted packets only. It never sees plaintext from the application layer above. On Linux/Windows a dedicated non-root service account plus `CAP_NET_ADMIN` (or the virtual service account) is enough to operate the TUN device and bind the socket. macOS is the exception: the OS gates `utun` creation to root, so the daemon runs as root there.

## Updates: how the service gets the new binary

The service outlives the app, so an app update replaces the sidecar file but not the running process.

- **The sidecar restarts itself.** It hashes its own executable at startup, reports it as `exe_sha256` in `GET /status`, and checks the file about every 30 seconds (`cmd/potluck-mesh/selfrestart.go`). When the file has been replaced and has been unchanged for 10 seconds, it shuts down as for SIGTERM and exits with code 75. `POTLUCK_MESH_NO_SELF_RESTART=1` turns the watcher off.
- **The service manager starts the new binary after a non-zero exit:**
  - macOS: `KeepAlive` is true in `ai.trypotluck.mesh.plist`, which restarts on any exit. `SMAppService.h` says a daemon whose executable changed must be registered again "or it may not launch", and recommends unregistering first. Whether launchd starts a replaced binary after a plain KeepAlive respawn has not been confirmed on a real Mac.
  - Linux: `Restart=on-failure` in the unit `mesh-install-helper.sh` writes.
  - Windows: the task has a restart-on-failure setting (three restarts). Whether Task Scheduler counts a non-zero exit code as a failure has not been confirmed, and a change that adds a keep-alive trigger to the task is waiting to be run on a real Windows machine. It does not matter for updates, see the next point.
- **The Windows service runs a copy** under `C:\ProgramData\Potluck\mesh-bin\`, which only the elevated helper replaces (after stopping the process), so the watcher does not fire there.
- **The app checks too.** `main.js` compares the running sidecar's `exe_sha256` with the hash of the binary it shipped and brings the service up to date by the first mechanism that applies (`mesh-service-update.js` decides):
  - **A sidecar with the restart command** (`restart_v1` in `GET /status` features) that still reports a different hash after 90 seconds gets `POST /restart` on its local socket. It shuts down and exits 75, and the service manager starts the new binary. No prompt. On macOS the service registration is refreshed first, with the signed helper's unregister then register, because `SMAppService.h` says a daemon whose executable changed must be registered again; on a real Mac that is what makes launchd run the new executable, and it needed no approval. Windows never uses the command.
  - **A sidecar with no `exe_sha256`** (every release before 0.1.11) has no restart command and never restarts itself. Nothing is automatic. The app reports the state `outdated` and shows "Restart mesh service". On Linux and Windows the button runs the elevated install. On macOS it refreshes the registration, then shows the system's administrator dialog ("Potluck needs to restart its mesh service to finish the update.") and sends SIGTERM to the one root-owned process that runs the service binary.
  - If the hash still differs 60 seconds after a restart command, the state is `outdated` and the button repeats it (macOS) or runs the elevated install (Linux).
- **`SMAppService.unregister()` does not stop a running daemon.** `SMAppService.h` says it kills it, but a real Mac kept the old process alive, so the app never relies on it. The old process holds the TUN device (`utun9`), so the new job `register` starts cannot create it. A sidecar with `cmd/potluck-mesh/tunnelwait.go` waits for the device and retries every 5 seconds instead of exiting 1 for launchd to start it again every 10 seconds.
- An AppImage cannot install the service (the helper refuses the mount path and is only installed by the `.deb`), so the app does nothing for it.

## Verification (post-install)

After installing on each platform, check the daemon is alive:

- **macOS:** `sudo launchctl print system/ai.trypotluck.mesh`
- **Linux:** `systemctl status potluck-mesh`
- **Windows:** `Get-Service -Name PotluckMesh`

All three should report "running." Logs:

- **macOS:** `tail -f /Library/Logs/Potluck/mesh.log`
- **Linux:** `journalctl -u potluck-mesh -f`
- **Windows:** Event Viewer → Applications and Services Logs → PotluckMesh
