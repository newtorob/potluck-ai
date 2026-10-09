'use strict';

// Decides whether the mesh service that is running is the one this app
// shipped, and what to do about it. No I/O and no Electron: main.js hashes the
// bundled binary, asks the service for GET /status, and passes both here along
// with the clock, so every branch can be tested with `node --test`.
//
// Why this exists: the mesh sidecar runs as a system service (macOS
// LaunchDaemon, Linux systemd unit, Windows SYSTEM task) that outlives the
// app. When the app updates, the binary on disk is replaced but the running
// process is still the old one. A sidecar new enough to carry exe_sha256 in
// /status also watches its own file and exits so the service manager starts
// the new binary (cmd/potluck-mesh/selfrestart.go); an older one never will.

// A sidecar that reports a hash different from the bundled binary's is
// normally on its way to restarting itself (the watcher notices the file within
// about 30 seconds and waits 10 more for it to settle). Give it this long, from
// when the app first saw the mismatch, before treating it as stuck.
const MISMATCH_GRACE_MS = 90_000;

// The exit code the sidecar uses for a restart it asked for (exitSelfRestart in
// selfrestart.go). The Windows task reports it as the last result while it
// waits to start the new process, which is not a failure.
const SELF_RESTART_EXIT_CODE = 75;

const SUPPORTED_PLATFORMS = new Set(['darwin', 'linux', 'win32']);

function normaliseHash(value) {
  return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

/**
 * @param {object} input
 * @param {string} input.platform         process.platform
 * @param {string|null} input.bundledSha256  SHA-256 of the sidecar binary this
 *        app shipped, or null when there is none to compare with (a development
 *        build, an unreadable file, an app that is not where the macOS service
 *        runs from).
 * @param {object|null} input.status      The parsed GET /status body, or null
 *        when the service did not answer.
 * @param {{sha256: string, at: number}|null} input.mismatchSeen  What the last
 *        call returned in mismatchSeen: the differing hash the service reported
 *        and when the app first saw it.
 * @param {number} input.now              Date.now() in milliseconds.
 * @param {boolean} input.macRestartAttempted  Whether the automatic macOS
 *        restart already ran in this app launch.
 * @param {boolean} [input.isAppImage]    Linux AppImage run, where the install
 *        helper refuses the mount path, so there is nothing to offer.
 * @returns {{
 *   verdict: 'unknown'|'current'|'settling'|'outdated',
 *   action: 'none'|'restart_mac'|'offer_restart',
 *   mismatchSeen: {sha256: string, at: number}|null,
 *   reason: string,
 * }}
 *   action 'restart_mac': restart the service now without asking (once).
 *   action 'offer_restart': report state 'outdated' to the renderer.
 */
function decideMeshServiceUpdate(input) {
  const { platform, bundledSha256, status, mismatchSeen, now, macRestartAttempted, isAppImage } = input;
  const result = (verdict, action, seen, reason) => ({ verdict, action, mismatchSeen: seen, reason });

  if (!SUPPORTED_PLATFORMS.has(platform)) {
    return result('unknown', 'none', null, 'unsupported platform');
  }
  const bundled = normaliseHash(bundledSha256);
  if (!bundled) {
    return result('unknown', 'none', null, 'no bundled sidecar to compare with');
  }
  if (platform === 'linux' && isAppImage) {
    return result('unknown', 'none', null, 'AppImage: the install helper cannot use the mount path');
  }
  if (status === null || typeof status !== 'object') {
    return result('unknown', 'none', null, 'service did not answer');
  }
  if (typeof status.mesh_status !== 'string') {
    return result('unknown', 'none', null, 'answer is not a mesh /status body');
  }

  const outdated = (seen, reason) =>
    result('outdated', platform === 'darwin' && !macRestartAttempted ? 'restart_mac' : 'offer_restart', seen, reason);

  // An older sidecar has no exe_sha256 at all and will never restart itself.
  const reported = status.exe_sha256;
  if (reported === undefined || reported === null || typeof reported !== 'string') {
    return outdated(null, 'service predates exe_sha256');
  }

  const running = normaliseHash(reported);
  if (running === '') {
    // A current sidecar that could not hash its own file. It cannot tell us
    // anything and does not watch itself, so there is nothing to compare.
    return result('unknown', 'none', null, 'service reported no hash');
  }
  if (running === bundled) {
    return result('current', 'none', null, 'service runs the bundled binary');
  }

  // Different hash. A new process with a different hash starts a new grace
  // period; the same hash keeps counting from when it was first seen.
  const seen = mismatchSeen && mismatchSeen.sha256 === running ? mismatchSeen : { sha256: running, at: now };
  if (now - seen.at < MISMATCH_GRACE_MS) {
    return result('settling', 'none', seen, 'service reports a different hash, waiting for it to restart itself');
  }
  return outdated(seen, 'service still runs a different binary after the grace period');
}

module.exports = {
  decideMeshServiceUpdate,
  MISMATCH_GRACE_MS,
  SELF_RESTART_EXIT_CODE,
  normaliseHash,
};
