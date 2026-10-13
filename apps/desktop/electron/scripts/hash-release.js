#!/usr/bin/env node
/**
 * Emit a SHA256SUMS file next to the built installers.
 *
 * Windows builds ship without a code signature, so a published hash is how a
 * user verifies integrity - the install page tells them to match it. This runs after every
 * `electron-builder` build (chained into the build:* npm scripts) so the hash
 * is always current with the artifacts. Node's crypto keeps it cross-platform
 * (the same script runs on the mac, windows, and linux build hosts).
 *
 * Verify later with:  shasum -a 256 -c SHA256SUMS   (or `sha256sum -c`)
 */
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

// electron-builder `directories.output`
const OUT_DIR = path.join(__dirname, '..', 'out');

// User-facing distributables only - skip updater metadata (.yml/.blockmap) and
// the unpacked app directories.
const ARTIFACT_EXTENSIONS = new Set([
  '.dmg',
  '.zip',
  '.exe',
  '.appimage',
  '.deb',
  '.rpm',
]);

function sha256(filePath) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    const stream = fs.createReadStream(filePath);
    stream.on('error', reject);
    stream.on('data', (chunk) => hash.update(chunk));
    stream.on('end', () => resolve(hash.digest('hex')));
  });
}

async function main() {
  if (!fs.existsSync(OUT_DIR)) {
    console.warn(`[hash-release] ${OUT_DIR} does not exist, nothing to hash.`);
    return;
  }

  const artifacts = fs
    .readdirSync(OUT_DIR, { withFileTypes: true })
    .filter((e) => e.isFile())
    .map((e) => e.name)
    .filter((name) => ARTIFACT_EXTENSIONS.has(path.extname(name).toLowerCase()))
    .sort();

  if (artifacts.length === 0) {
    console.warn('[hash-release] No installer artifacts found in out/, skipping.');
    return;
  }

  const lines = [];
  for (const name of artifacts) {
    const digest = await sha256(path.join(OUT_DIR, name));
    // coreutils format: "<hash><space><space><filename>" so `-c` can verify.
    lines.push(`${digest}  ${name}`);
    console.log(`[hash-release] ${digest}  ${name}`);
  }

  const sumsPath = path.join(OUT_DIR, 'SHA256SUMS');
  fs.writeFileSync(sumsPath, lines.join('\n') + '\n');
  console.log(`[hash-release] Wrote ${sumsPath} (${artifacts.length} artifact(s)).`);
}

main().catch((err) => {
  console.error(`[hash-release] Failed: ${err.message}`);
  process.exit(1);
});
