#!/usr/bin/env bash
# build.sh — compile + codesign the mesh-helper Swift CLI.
#
# Produces a universal (arm64 + x86_64) Mach-O binary at
# apps/desktop/electron/resources/mesh-helper/mesh-helper, matching the two
# mac architectures electron-builder targets (see package.json's
# mac.target arch list). electron-builder's mac.extraFiles then places it at
# Contents/MacOS/mesh-helper — a REQUIRED sibling location of the main
# Electron executable; see the header comment in main.swift for why.
#
# Invoked from the electron prepack step (package.json's prepack:mesh-helper).
#
# Environment:
#   CODESIGN_IDENTITY — signing identity. Default: the Potluck Developer ID
#                        Application identity. Set to "-" for an ad-hoc dev
#                        build (no cert required, but SMAppService will refuse
#                        to register a daemon whose Team ID doesn't match the
#                        calling app's — ad-hoc builds cannot register a real
#                        LaunchDaemon, only compile/run `status` locally).
#
# Non-fatal signing: if codesign fails (locked keychain, missing cert, CI
# host without the identity), this script logs a loud warning and leaves the
# unsigned/ad-hoc binary in place rather than failing the whole prepack step
# — matches the "leave the signing step wired but document that it runs at
# package time" instruction. A real release build MUST have this succeed.

set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "[mesh-helper] Non-macOS platform ($(uname)) — skipping (macOS-only mechanism for now)."
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$SCRIPT_DIR/../../resources/mesh-helper"
BIN_NAME="mesh-helper"
MIN_MACOS="13.0"  # SMAppService requires macOS 13+.

IDENTITY="${CODESIGN_IDENTITY:-Developer ID Application: Example (TEAMID)}"

mkdir -p "$OUT_DIR"

echo ""
echo "=== mesh-helper build ==="
echo "  Source:    $SCRIPT_DIR/main.swift"
echo "  Output:    $OUT_DIR/$BIN_NAME"
echo "  Identity:  $IDENTITY"
echo ""

echo "[mesh-helper] Compiling arm64 slice..."
swiftc -O -target "arm64-apple-macos${MIN_MACOS}" \
  -o "$OUT_DIR/${BIN_NAME}-arm64" \
  "$SCRIPT_DIR/main.swift"

echo "[mesh-helper] Compiling x86_64 slice..."
swiftc -O -target "x86_64-apple-macos${MIN_MACOS}" \
  -o "$OUT_DIR/${BIN_NAME}-x86_64" \
  "$SCRIPT_DIR/main.swift"

echo "[mesh-helper] Combining into a universal binary..."
lipo -create -output "$OUT_DIR/$BIN_NAME" \
  "$OUT_DIR/${BIN_NAME}-arm64" \
  "$OUT_DIR/${BIN_NAME}-x86_64"
rm -f "$OUT_DIR/${BIN_NAME}-arm64" "$OUT_DIR/${BIN_NAME}-x86_64"

chmod +x "$OUT_DIR/$BIN_NAME"

echo "[mesh-helper] Codesigning with identity: $IDENTITY"
if codesign --sign "$IDENTITY" --force --options runtime --timestamp "$OUT_DIR/$BIN_NAME" 2>&1; then
  if codesign --verify --strict "$OUT_DIR/$BIN_NAME" 2>&1; then
    echo "[mesh-helper] Signed + verified OK."
  else
    echo "[mesh-helper] WARNING: codesign succeeded but strict verify failed." >&2
  fi
else
  echo "" >&2
  echo "[mesh-helper] WARNING: codesign FAILED (missing cert, locked keychain, or" >&2
  echo "  no UI session to unlock it). Leaving an UNSIGNED binary in place." >&2
  echo "  This is fine for a local ad-hoc test of the 'status' subcommand, but" >&2
  echo "  SMAppService.register() will be REJECTED for an unsigned/mismatched-" >&2
  echo "  Team-ID helper in a real packaged build — this MUST succeed before" >&2
  echo "  producing a release artifact. Re-run this script on a machine with the" >&2
  echo "  Developer ID identity unlocked in Keychain Access." >&2
  echo "" >&2
fi

echo ""
echo "[mesh-helper] Done: $OUT_DIR/$BIN_NAME"
lipo -info "$OUT_DIR/$BIN_NAME" 2>/dev/null || true
echo ""
