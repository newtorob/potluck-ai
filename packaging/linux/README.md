# Linux app-driven mesh install (polkit)

How the Potluck desktop app installs and starts `potluck-mesh` as a privileged
systemd service on Linux, without a terminal or `sudo`. It is the Linux
counterpart to macOS's `SMAppService` root LaunchDaemon (see the comment block
above the mesh-daemon section in `apps/desktop/electron/main.js`).

## Files

| File | Installed to | Purpose |
|---|---|---|
| `polkit/ai.trypotluck.mesh.policy` | `/usr/share/polkit-1/actions/ai.trypotluck.mesh.policy` | polkit action definition. Binds the `ai.trypotluck.mesh.install` action id to the one fixed helper path below via `org.freedesktop.policykit.exec.path`, giving a branded auth prompt instead of pkexec's generic one. |
| `mesh-install-helper.sh` | `/usr/lib/potluck/mesh-install-helper.sh` (root:root, 0755) | The only thing pkexec is authorized to run for this action. Renders, installs and starts a root systemd unit for `potluck-mesh`. See its own header comment for the full security model. |
| `deb-postinst.sh` | run by dpkg after the `.deb` is unpacked | Stages the two files above from the app's resources and restarts polkit so the action id is visible. |

The `.deb` installs both files at those exact paths. The policy file's
`org.freedesktop.policykit.exec.path` annotation and `main.js`'s
`getLinuxMeshHelperPath()` both hardcode `/usr/lib/potluck/mesh-install-helper.sh`.
`getLinuxMeshHelperPath()` returns nothing for an unpackaged (dev) app, so the
app-driven install is available only in a packaged build.

## How it's wired into the app

`apps/desktop/electron/main.js`, Linux branch of `mesh:daemon-status` /
`mesh:enable`:

- `mesh:daemon-status` never needs root. It asks `systemctl is-active` /
  `systemctl list-unit-files` directly (`computeLinuxDaemonStatus()`), then
  probes the local IPC socket exactly like the macOS path does.
- `mesh:enable` runs `pkexec /usr/lib/potluck/mesh-install-helper.sh install`
  with a fixed argument list (the default coordinator and DERP URLs, the
  packaged mesh binary path, and this Electron process's own uid/gid). No
  renderer input reaches this call.

The frontend (`MeshEnablePanel.tsx`, `meshStore.ts`) uses the same
`not_installed` / `starting` / `running` states on every platform. Only the
approval-prompt copy depends on the platform (`daemonStatus.platform`).

## Why the daemon runs as root

`scripts/systemd/install-persistence.sh` (the headless server installer,
separate from this app-driven flow) runs `potluck-mesh` as a dedicated
non-root `potluck` system user with
`AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW`. That is the minimum-privilege
shape for an unattended server where no desktop app needs the IPC socket.

The app-driven install on a desktop needs a different shape: the logged-in
user must be able to open the mesh's local IPC socket the moment the daemon
starts, with no extra "log out and back in" step. A shared-group scheme
(`potluck` group + `chmod 0660`) would require exactly that, because Linux
resolves a process's supplementary groups at login, not per `open()` call, so
an already-running Electron process would not see a new group membership until
the user's session restarts. Running as root and reusing the reconciliation
code the macOS LaunchDaemon already depends on
(`internal/localapi/listener_unix.go`'s `platformListen()`, which chowns the
socket to `SUDO_UID`/`SUDO_GID`, or failing that to the active console/login
user via `loginctl`/`who`/`logname`) gives immediate access. The helper passes
`SUDO_UID`/`SUDO_GID` explicitly, set to the invoking Electron process's own
uid/gid. That is the user who clicked "Enable", so the socket ends up owned by
the right user without relying on session-manager heuristics.

The difference from the headless installer is intentional. See the long
comment at the top of `mesh-install-helper.sh` for the full reasoning.

## Fixed socket path

`POTLUCK_LOCAL_API_SOCKET=/run/potluck-mesh.sock` is a flat path under `/run`
that does not depend on any login session. A per-user path
(`$XDG_RUNTIME_DIR` or `/run/user/$UID`) may not exist yet when a system
service starts at boot, and it is torn down and recreated on every re-login.
`apps/desktop/electron/main.js`'s `meshSocketPath()` checks the fixed path
first on Linux and falls back to the per-user path only for a manually run dev
binary. The unattended unit template `packaging/daemons/potluck-mesh.service`
declares the same location (`/var/run` is a symlink to `/run` on systemd
distributions), so both install paths use one canonical socket.

## Socket access from the desktop user

The mesh daemon creates its IPC socket with mode `0600`. It chowns the socket
to another user only when it runs as root
(`internal/localapi/listener_unix.go`). So the account that owns the socket
decides who can use it:

- With the headless installer, the service runs as the non-root `potluck`
  user and `XDG_RUNTIME_DIR=/run/potluck-mesh`, so the socket is at
  `/run/potluck-mesh/potluck-mesh.sock`, owned by `potluck`. A desktop app
  that looks for a per-user path does not find it there, and a different
  non-root user cannot open it.
- With the app-driven install, the service runs as root with the fixed path
  above and the socket is chowned to the desktop user, so the app reaches it
  without `sudo`.

Windows has the same class of question when the daemon does not run as the
interactive user; see the header comment of
`internal/localapi/listener_windows.go`.

## Staging the files by hand

The `.deb` postinst stages the helper and the polkit action automatically.
For a build that does not come from the `.deb`, stage them by hand once per
machine:

```bash
sudo mkdir -p /usr/lib/potluck
sudo install -m 0755 -o root -g root packaging/linux/mesh-install-helper.sh \
    /usr/lib/potluck/mesh-install-helper.sh
sudo install -m 0644 -o root -g root packaging/linux/polkit/ai.trypotluck.mesh.policy \
    /usr/share/polkit-1/actions/ai.trypotluck.mesh.policy
sudo systemctl restart polkit   # picks up the new action; confirm with:
pkaction --action-id ai.trypotluck.mesh.install -v
```

`ALLOWED_MESH_BIN_PREFIXES` in `mesh-install-helper.sh` lists the directories
the helper accepts a mesh binary from: `/opt/Potluck AI/resources/mesh/` (the
`.deb` layout), `/usr/lib/potluck/mesh/` and, as a development allowance,
`/usr/local/lib/potluck/`. The last one is where the headless installer stages
its binary, so the helper can be tested against that binary.

A pkexec/polkit authentication dialog needs a desktop session and an
authentication agent (the one every GNOME or KDE session runs). It cannot be
driven over a headless SSH connection. The privileged install logic itself
runs the same way when invoked as root directly:

```bash
sudo /usr/lib/potluck/mesh-install-helper.sh install --coordinator-url <url> \
    --derp-url <url> --uid <uid> --gid <gid>
sudo /usr/lib/potluck/mesh-install-helper.sh status
sudo /usr/lib/potluck/mesh-install-helper.sh uninstall
```
