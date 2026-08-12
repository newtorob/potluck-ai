#!/usr/bin/env bash
# Publish a freshly-built macOS release to the R2 bucket behind
# releases.trypotluck.ai so the in-app auto-updater can find it.
#
# The desktop app updates via electron-updater with a `generic` provider
# pointed at https://releases.trypotluck.ai (apps/desktop/electron/package.json
# -> build.publish). For that to work, the bucket ROOT must serve:
#   - latest-mac.yml            the update feed electron-updater polls
#   - *-mac.zip (+ .blockmap)   the artifact the updater downloads & applies
#   - *.dmg     (+ .blockmap)   the fresh-install download (install page link)
# electron-builder emits all of these into apps/desktop/electron/out/ during a
# full `make installer-mac`.
#
# PREREQS
#   1. A signed + notarized build:
#        make installer-mac
#      (needs APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / APPLE_TEAM_ID in the
#      environment + the "Developer ID Application" cert in the login keychain.
#      Unsigned builds must NOT be published: macOS auto-update via Squirrel.Mac
#      only applies SIGNED updates, and app-update.yml is only emitted by this
#      full build, not the `--dir` dogfood build.)
#   2. An rclone remote for R2 (configure once, with your Cloudflare R2 creds):
#        rclone config create r2 s3 \
#          provider=Cloudflare \
#          access_key_id=<R2_ACCESS_KEY_ID> \
#          secret_access_key=<R2_SECRET_ACCESS_KEY> \
#          endpoint=https://<CF_ACCOUNT_ID>.r2.cloudflarestorage.com \
#          acl=private
#   3. The bucket name mapped to releases.trypotluck.ai.
#
# USAGE
#   scripts/release_upload.sh <rclone-remote> <bucket> [--dry-run]
#     e.g. scripts/release_upload.sh r2 potluck-releases
#
# Idempotent: re-running re-uploads changed files. Safe to run for every release.
set -euo pipefail

REMOTE="${1:?rclone remote name (e.g. r2) — see PREREQS #2}"
BUCKET="${2:?R2 bucket name mapped to releases.trypotluck.ai — see PREREQS #3}"
DRY="${3:-}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO_ROOT/apps/desktop/electron/out"

if [ ! -d "$OUT" ]; then
  echo "error: no build output at $OUT" >&2
  echo "       run a full signed build first:  make installer-mac" >&2
  exit 1
fi

# Guard: refuse to publish if the feed file is missing (a --dir/dogfood build
# produces no latest-mac.yml, so publishing it would give users a broken feed).
if [ -z "$(find "$OUT" -maxdepth 1 -name 'latest*.yml' -print -quit)" ]; then
  echo "error: no latest-mac.yml in $OUT — this is not a full 'make installer-mac' build." >&2
  echo "       (a '--dir'/dogfood build cannot be published; it has no update feed.)" >&2
  exit 1
fi

echo "==> Artifacts to publish from $OUT:"
find "$OUT" -maxdepth 1 \( -name '*.dmg' -o -name '*.zip' -o -name '*.blockmap' -o -name 'latest*.yml' \) \
  -exec basename {} \; | sort | sed 's/^/    /'

# Plain string (not an array): macOS bash 3.2 errors on "${arr[@]}" for an
# empty array under `set -u`. Empty -> no flag; word-splits to --dry-run when set.
DRY_FLAG=""
if [ "$DRY" = "--dry-run" ]; then
  DRY_FLAG="--dry-run"
  echo "==> DRY RUN — nothing will be uploaded."
fi

echo "==> Uploading to ${REMOTE}:${BUCKET} (bucket root)"
# shellcheck disable=SC2086 -- DRY_FLAG is intentionally unquoted (empty or --dry-run)
rclone copy "$OUT" "${REMOTE}:${BUCKET}" \
  --max-depth 1 \
  --include "*.dmg" \
  --include "*.zip" \
  --include "*.blockmap" \
  --include "latest*.yml" \
  --progress $DRY_FLAG

echo ""
echo "==> Done. Verify the feed is live:"
echo "      curl -sI https://releases.trypotluck.ai/latest-mac.yml"
echo "    and that it lists the version you just built."
