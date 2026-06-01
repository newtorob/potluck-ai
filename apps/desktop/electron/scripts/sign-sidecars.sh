#!/usr/bin/env bash
# sign-sidecars.sh — codesign every Mach-O inside a PyInstaller onedir bundle.
#
# Usage:
#   ./sign-sidecars.sh <bundle-dir>
#
# The bundle-dir is the top-level PyInstaller output directory, e.g.:
#   apps/desktop/python/dist/potluck-local/
#
# Environment:
#   CODESIGN_IDENTITY  — the signing identity to use.
#                        Default: "-" (ad-hoc, no network, no cert required).
#                        Production: "Developer ID Application: Potluck AI, Inc. (TEAMID)"
#
#   ENTITLEMENTS_PLIST — path to the entitlements plist.
#                        Default: <this script's dir>/../entitlements.plist
#
# The script is idempotent: re-signing an already-signed binary is safe.
# Exits non-zero on the first codesign or verify failure.
#
# Compatible with bash 3.2 (macOS system bash).

set -euo pipefail

# ── Args ──────────────────────────────────────────────────────────────────────
if [ $# -lt 1 ]; then
  echo "Usage: $0 <bundle-dir>" >&2
  exit 1
fi

BUNDLE_DIR="$(cd "$1" && pwd)"

if [ ! -d "$BUNDLE_DIR" ]; then
  echo "ERROR: bundle directory does not exist: $BUNDLE_DIR" >&2
  exit 1
fi

# ── Config ────────────────────────────────────────────────────────────────────
IDENTITY="${CODESIGN_IDENTITY:--}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENTITLEMENTS="${ENTITLEMENTS_PLIST:-$SCRIPT_DIR/../entitlements.plist}"

if [ ! -f "$ENTITLEMENTS" ]; then
  echo "ERROR: entitlements plist not found: $ENTITLEMENTS" >&2
  exit 1
fi

AD_HOC=false
if [ "$IDENTITY" = "-" ]; then
  AD_HOC=true
fi

echo ""
echo "=== sign-sidecars.sh ==="
echo "  Bundle:       $BUNDLE_DIR"
echo "  Identity:     $IDENTITY"
echo "  Entitlements: $ENTITLEMENTS"
echo "  Ad-hoc:       $AD_HOC"
echo ""

# ── Collect all Mach-O files ──────────────────────────────────────────────────
# Use `file` to detect actual Mach-O rather than relying on extension names.
# Write to a temp file so we can count and iterate without subshell scoping issues.
TMPFILE="$(mktemp /tmp/macho-files.XXXXXX)"
trap 'rm -f "$TMPFILE"' EXIT

find "$BUNDLE_DIR" -type f -print0 \
  | xargs -0 file \
  | grep -E 'Mach-O' \
  | cut -d: -f1 \
  | sort > "$TMPFILE"

TOTAL=$(wc -l < "$TMPFILE" | tr -d ' ')

echo "Found $TOTAL Mach-O file(s) to sign."
echo ""

if [ "$TOTAL" -eq 0 ]; then
  echo "WARNING: no Mach-O files found in $BUNDLE_DIR — nothing signed." >&2
  exit 0
fi

# ── Sign ──────────────────────────────────────────────────────────────────────
FAILED=0
COUNT=0

while IFS= read -r f; do
  COUNT=$((COUNT + 1))
  echo "[$COUNT/$TOTAL] Signing: $f"

  if [ "$AD_HOC" = "true" ]; then
    # Ad-hoc mode: no --timestamp (requires network + Developer ID), no --options
    # runtime (hardened runtime requires a real cert to be verifiable).
    # --force overwrites any existing signature (idempotent).
    if ! codesign \
      --sign "-" \
      --force \
      --entitlements "$ENTITLEMENTS" \
      "$f" 2>&1; then
      echo "  FAILED to sign: $f" >&2
      FAILED=$((FAILED + 1))
    fi
  else
    # Production: hardened runtime + secure timestamp (both required for notarization).
    if ! codesign \
      --sign "$IDENTITY" \
      --force \
      --options runtime \
      --entitlements "$ENTITLEMENTS" \
      --timestamp \
      "$f" 2>&1; then
      echo "  FAILED to sign: $f" >&2
      FAILED=$((FAILED + 1))
    fi
  fi
done < "$TMPFILE"

echo ""

if [ "$FAILED" -gt 0 ]; then
  echo "ERROR: $FAILED file(s) failed to sign." >&2
  exit 1
fi

# ── Verify ────────────────────────────────────────────────────────────────────
echo "Verifying all signed files..."
VFY_FAILED=0

while IFS= read -r f; do
  if ! codesign --verify --strict "$f" 2>&1; then
    echo "  VERIFY FAILED: $f" >&2
    VFY_FAILED=$((VFY_FAILED + 1))
  fi
done < "$TMPFILE"

echo ""

if [ "$VFY_FAILED" -gt 0 ]; then
  echo "ERROR: $VFY_FAILED file(s) failed strict verification." >&2
  exit 1
fi

echo "All $TOTAL Mach-O file(s) signed and verified successfully."
echo ""
