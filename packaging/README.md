# Packaging: Potluck desktop installers

How the Potluck desktop app is bundled into installers for macOS, Windows and Linux. The formats come from the `build` section of `apps/desktop/electron/package.json`:

| OS | Formats |
|---|---|
| macOS | `.dmg` and `.zip` (arm64) |
| Windows | NSIS `.exe` (x64) |
| Linux | `.AppImage` and `.deb` (x64) |

## Architecture

A Potluck installer bundles these parts:

1. **Electron desktop app** (`apps/desktop/electron/` + `apps/desktop/frontend/`): the user-facing UI. JavaScript + React.
2. **Python sidecars** (`apps/desktop/python/`): `potluck-local` (the local memory provider, agent observability and model management) and `potluck-mcp` (the MCP server). Both are bundled with PyInstaller as `--onedir` directories (`build.py`, `build-mcp.py`), so no Python installation is needed on the user's machine.
3. **Go mesh sidecar** (`apps/desktop/go/potluck-mesh/`): the WireGuard mesh, DERP client and local IPC server. A Go binary built per platform.
4. **Mesh-service install helpers**, one per OS, so the mesh sidecar can run as a system service that survives logout:
   - macOS: a static LaunchDaemon plist (`packaging/daemons/ai.trypotluck.mesh.plist`) placed in `Contents/Library/LaunchDaemons/` of the app bundle, plus the Swift `mesh-helper` (`apps/desktop/electron/native/mesh-helper/`) at `Contents/MacOS/mesh-helper`.
   - Linux: `packaging/linux/mesh-install-helper.sh` and the polkit action in `packaging/linux/polkit/`, staged by the `.deb` postinst (`packaging/linux/deb-postinst.sh`). See [`linux/README.md`](./linux/README.md).
   - Windows: `packaging/windows/mesh-install-helper.ps1`.

   The service templates are described in [`daemons/README.md`](./daemons/README.md).

## Build pipeline

```
   apps/desktop/python/ ──build.py, build-mcp.py──> resources/python/
                                                      potluck-local/
                                                      potluck-mcp/        ─┐
                                                                          │
   apps/desktop/go/potluck-mesh/ ──make build──────> resources/mesh/      │
                                                      potluck-mesh        ─┤
                                                                          │
   apps/desktop/electron/native/mesh-helper/ ─────> resources/mesh-helper/ ┤ (macOS)
                                                                          │
   apps/desktop/frontend/ ──vite build──> frontend/dist/                  ─┤
                                                                          ▼
   apps/desktop/electron/ ──electron-builder──> out/  (.dmg, .zip, .exe,
                                                        .AppImage, .deb,
                                                        update feed files)
                                                       + SHA256SUMS
```

`electron-builder` copies the platform install helpers from `packaging/` into the app (the `extraResources` and `extraFiles` entries in `package.json`). `scripts/hash-release.js` runs after each `build:*` script and writes a `SHA256SUMS` file next to the installers.

## Make targets

All targets run from the repo root.

| Target | What it does |
|---|---|
| `make installer-deps` | Install the build tools from `requirements-build.txt` and check that `go` and `pnpm` are present |
| `make installer-bundle` | Build the Python sidecars and the Go mesh sidecar, build the frontend, and stage everything into `apps/desktop/electron/resources/` |
| `make installer-mac` | Build the macOS `.dmg` and `.zip`. Signs with the Developer ID certificate in the login keychain, and notarizes when the `APPLE_*` variables below are set. Without a certificate the build is unsigned. |
| `make installer-mac-dir` | Fast unsigned macOS build: the unpacked `.app` only, no `.dmg`, `.zip` or notarization |
| `make installer-win` | Build the Windows NSIS `.exe`. The build is not code-signed. |
| `make installer-linux` | Build the Linux installers with `electron-builder --linux` |
| `make installer-linux-docker` | Build the Linux `.deb` and `.AppImage` (x64) inside a Docker container from a clean checkout of `HEAD`. Output goes to `dist-linux/out/`. |
| `make installer-clean` | Remove staged resources and `electron-builder` output |
| `make install-mac-app` | Build the unpacked `.app` and install it to `/Applications` |
| `make dogfood` | Kill running Potluck processes, build, install to `/Applications` and print the installed build stamp |

Run `make installer-mac`, `make installer-win` and `make installer-linux` on the matching host OS. PyInstaller cannot cross-compile, and `electron-builder`'s `beforePack` check (`scripts/check-native-architecture.js`) refuses a macOS installer whose native sidecars do not match the target CPU. `make installer-linux-docker` builds the Linux packages on any host with Docker.

On Windows, `scripts/build-windows-installer.ps1` runs the same steps without the POSIX shell chain that the Makefile uses.

## Signing

### macOS

- Sign with a **Developer ID Application** certificate in the login keychain. `CODESIGN_IDENTITY` selects the identity for the sidecar signing step (`scripts/sign-sidecars.sh`; the default `-` is an ad hoc signature) and for the Swift helper build.
- Notarization runs as the `electron-builder` `afterSign` hook (`apps/desktop/electron/scripts/notarize.js`) when these variables are set. If any is missing, notarization is skipped and the build succeeds.
  - `APPLE_ID`: the Apple ID email
  - `APPLE_APP_SPECIFIC_PASSWORD`: an app-specific password
  - `APPLE_TEAM_ID`: the 10-character Developer Team ID

See [`apps/desktop/electron/RELEASE.md`](../apps/desktop/electron/RELEASE.md) for the full macOS release steps.

### Windows

The Windows installer is built without a code signature. Publish the `SHA256SUMS` file with it so users can check their download. Windows SmartScreen can warn about an unsigned installer.

### Linux

The Linux packages are not signed by the build. Use `SHA256SUMS` to verify a download.

### Verifying a download

```bash
shasum -a 256 -c SHA256SUMS      # macOS
sha256sum -c SHA256SUMS          # Linux
```

```powershell
Get-FileHash -Algorithm SHA256 .\<installer>.exe
```

## Auto-update

`electron-updater` (a dependency in `apps/desktop/electron/package.json`) checks for updates in the packaged app. The `build.publish` setting uses the `generic` provider with the update feed at `https://releases.trypotluck.ai`. A full (non-`--dir`) build writes the feed files (`latest-mac.yml`, `latest.yml`, `latest-linux.yml`) next to the installers in `apps/desktop/electron/out/`. `scripts/release_upload.sh` uploads installers first and the feed second, so a feed never points at a file that is not there yet.

## One installer per platform

The mesh sidecar, the Python sidecars and the Electron app ship in a single installer per platform. A user installs one package and does not install Tailscale, Python or anything else first.

## Trying a local build

```bash
# Repo root, macOS.
make installer-mac-dir     # unsigned, unpacked app at apps/desktop/electron/out/mac-arm64/Potluck AI.app
make dogfood               # rebuild, replace /Applications/Potluck AI.app, print the build stamp
```

`make installer-bundle` stages the sidecars on its own if you only want to check the bundle step.
