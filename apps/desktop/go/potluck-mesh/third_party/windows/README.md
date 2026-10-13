# wintun.dll

`wintun.dll` is a runtime dependency of `golang.zx2c4.com/wireguard/tun` on
Windows, it is loaded via `LoadLibrary` at `tun.CreateTUN()` time
(`internal/tunnel/tunnel.go`), not linked at Go compile time, so it does not
show up in `go.mod`/`go.sum` and must be shipped as a sibling file next to
`potluck-mesh.exe`.

## Provenance

- Source: https://www.wintun.net/ (the WireGuard project's official Wintun
  driver distribution, same DLL used by `wireguard-windows` and Tailscale's
  Windows client).
- Version: 0.14.1 (`FileVersion`/`ProductVersion`, confirmed via
  `Get-Item ... | Select VersionInfo` on a real Windows box).
- Signed: Authenticode-signed by "WireGuard LLC" (DigiCert EV Code Signing
  CA). The signing certificate's own validity window has since lapsed, but
  the signature carries a valid trusted timestamp from when it was signed:
  `Get-AuthenticodeSignature` reports `Status: Valid`. It matches the copy
  Tailscale ships at `C:\Program Files\Tailscale\wintun.dll`, a widely
  deployed build.
- SHA-256: `e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce`

## How it's used

- `Makefile`'s `build`/`_build` targets copy this file into `dist/` alongside
  the built `potluck-mesh.exe` whenever the target OS is `windows`.
- `apps/desktop/electron/package.json`'s `prepack:mesh` script copies
  whatever lands in `dist/` (including `wintun.dll`, when present) into
  `resources/mesh/`, which electron-builder's `extraResources` then bundles
  into the packaged app at `resources/mesh/wintun.dll`, a sibling of
  `resources/mesh/potluck-mesh.exe`.
- `packaging/windows/mesh-install-helper.ps1` requires `wintun.dll` to exist
  next to the source `potluck-mesh.exe` it's given, and copies both into the
  admin-owned install directory (`C:\ProgramData\Potluck\mesh-bin\`) so the
  SYSTEM-run scheduled task can load it.

## Follow-up (not blocking launch)

Pin a specific upstream release + re-verify the SHA-256 against an official
wintun.net release archive as part of the CI pipeline, rather than trusting
this checked-in copy indefinitely. This is not done yet.
