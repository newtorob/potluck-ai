'use strict';

/**
 * generate-build-info.js — writes build-info.json into the electron package
 * directory (apps/desktop/electron/) at build time.
 *
 * This is the source of the staleness-detection stamp: the git short SHA the
 * tree was built from, an ISO-8601 build timestamp, and a dirty flag (were
 * there uncommitted changes at build time). main.js reads this file when
 * app.isPackaged and exposes it to the renderer via preload's
 * window.potluck.app.buildInfo — see main.js's loadBuildInfo().
 *
 * Run this BEFORE electron-builder so build-info.json exists when the "files"
 * glob in package.json picks it up. Wired into:
 *   - package.json's "prepack" script chain (prepack:build-info)
 *   - the root Makefile's installer-bundle target
 *
 * Never throws: a git failure (e.g. building from a tree with no .git, or a
 * shallow CI checkout without history) degrades to sha:"unknown" rather than
 * failing the whole build — a missing stamp is a much smaller problem than a
 * blocked release build.
 */

const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ELECTRON_ROOT = path.join(__dirname, '..');
const REPO_ROOT = path.join(ELECTRON_ROOT, '..', '..', '..');

function git(args) {
  return execFileSync('git', args, { cwd: REPO_ROOT, encoding: 'utf8' }).trim();
}

let sha = 'unknown';
try {
  sha = git(['rev-parse', '--short', 'HEAD']);
} catch (err) {
  console.warn(`[build-info] git rev-parse failed, sha=unknown: ${err.message}`);
}

let dirty = false;
try {
  dirty = git(['status', '--porcelain']).length > 0;
} catch (err) {
  console.warn(`[build-info] git status failed, dirty=false: ${err.message}`);
}

const info = {
  sha,
  builtAt: new Date().toISOString(),
  dirty,
};

const outPath = path.join(ELECTRON_ROOT, 'build-info.json');
fs.writeFileSync(outPath, `${JSON.stringify(info, null, 2)}\n`);
console.log(`[build-info] wrote ${outPath}`);
console.log(`[build-info] ${JSON.stringify(info)}`);
