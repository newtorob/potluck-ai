'use strict';

// Decides whether the mesh service that is running is the one this app shipped,
// and how to bring it up to date. No I/O and no Electron: main.js hashes the
// bundled binary, asks the service for GET /status, and passes both here along
// with the clock, so every branch can be tested with `node --test`.
//
// Why this exists: the mesh sidecar runs as a system service (macOS
// LaunchDaemon, Linux systemd unit, Windows SYSTEM task) that outlives the app.
// When the app updates, the binary on disk is replaced but the running process
// is still the old one. Three generations of sidecar matter:
//
//   - No exe_sha256 in /status: every sidecar before the one that added it. It
//     never restarts itself and has no restart command.
//   - exe_sha256 but no "restart_v1" feature: it watches its own file and exits
//     when it is replaced (cmd/potluck-mesh/selfrestart.go), which only helps if
//     the service manager then starts the new binary. It cannot be asked to
//     restart.
//   - "restart_v1" in features: it also answers POST /restart on its local
//     socket by shutting down and exiting 75, which any service manager that
//     restarts on a non-zero exit answers by starting the new binary.
//
// The result says which mechanism to use and whether to use it now:
//
//   'api_restart'        POST /restart to the running sidecar. On macOS the
//                        service registration is refreshed first (unregister,
//                        then register) so launchd's job points at the new
//                        executable. No prompt.
//   'mac_admin_restart'  macOS, a sidecar that cannot be asked to restart: the
//                        registration is refreshed, then the user is asked for
//                        administrator rights to send SIGTERM to the old
//                        process. Only ever behind the button.
//   'offer_restart'      Linux and Windows: the elevated install helper, which
//                        restarts the service. Only ever behind the button.
//
// `automatic` is true when main.js should run the mechanism now without being
// asked. `surface` is true when the renderer should be told the state is
// 'outdated' (the banner with its button). Both are never true together.

// A sidecar that reports a hash different from the bundled binary's is
// normally on its way to restarting itself (the watcher notices the file within
// about 30 seconds and waits 10 more for it to settle). Give it this long, from
// when the app first saw the mismatch, before acting.
const MISMATCH_GRACE_MS = 90_000;

// How long after an API restart the sidecar may still report the old hash
// before the state is surfaced. main.js waits this long for the new hash.
const API_RESTART_WINDOW_MS = 60_000;

// Automatic API restarts per app launch, across all hashes. Never a loop.
const AUTO_RESTART_LIMIT = 2;

// The exit code the sidecar uses for a restart it asked for (exitSelfRestart in
// selfrestart.go). The Windows task reports it as the last result while it
// waits to start the new process, which is not a failure.
const SELF_RESTART_EXIT_CODE = 75;

// The /status feature that says POST /restart exists (FeatureRestartV1 in
// internal/localapi/server.go).
const FEATURE_RESTART_V1 = 'restart_v1';

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
 * @param {{sha256: string, at: number}|null} input.apiRestart  The last API
 *        restart main.js started: the hash the sidecar reported at the time and
 *        when POST /restart was sent.
 * @param {number} input.autoRestarts     Automatic API restarts started so far
 *        in this launch.
 * @param {boolean} [input.isAppImage]    Linux AppImage run, where the install
 *        helper refuses the mount path, so there is nothing to offer.
 * @returns {{
 *   verdict: 'unknown'|'current'|'settling'|'outdated',
 *   mechanism: 'none'|'api_restart'|'mac_admin_restart'|'offer_restart',
 *   automatic: boolean,
 *   surface: boolean,
 *   running: string,
 *   mismatchSeen: {sha256: string, at: number}|null,
 *   reason: string,
 * }}
 */
function decideMeshServiceUpdate(input) {
  const { platform, bundledSha256, status, mismatchSeen, now, apiRestart, autoRestarts, isAppImage } = input;
  const result = (verdict, mechanism, flags, seen, reason, running = '') => ({
    verdict,
    mechanism,
    automatic: Boolean(flags.automatic),
    surface: Boolean(flags.surface),
    running,
    mismatchSeen: seen,
    reason,
  });
  const none = (reason) => result('unknown', 'none', {}, null, reason);

  if (!SUPPORTED_PLATFORMS.has(platform)) return none('unsupported platform');
  const bundled = normaliseHash(bundledSha256);
  if (!bundled) return none('no bundled sidecar to compare with');
  if (platform === 'linux' && isAppImage) {
    return none('AppImage: the install helper cannot use the mount path');
  }
  if (status === null || typeof status !== 'object') return none('service did not answer');
  if (typeof status.mesh_status !== 'string') return none('answer is not a mesh /status body');

  // What the button does when nothing can be done without asking.
  const askedMechanism = platform === 'darwin' ? 'mac_admin_restart' : 'offer_restart';

  // An older sidecar has no exe_sha256 at all, never restarts itself and has no
  // restart command. Nothing automatic; the button is the way out.
  const reported = status.exe_sha256;
  if (reported === undefined || reported === null || typeof reported !== 'string') {
    return result('outdated', askedMechanism, { surface: true }, null, 'service predates exe_sha256');
  }

  const running = normaliseHash(reported);
  if (running === '') {
    // A current sidecar that could not hash its own file. It cannot tell us
    // anything and does not watch itself, so there is nothing to compare.
    return none('service reported no hash');
  }
  if (running === bundled) {
    return result('current', 'none', {}, null, 'service runs the bundled binary', running);
  }

  // Different hash. A new process with a different hash starts a new grace
  // period; the same hash keeps counting from when it was first seen.
  const seen = mismatchSeen && mismatchSeen.sha256 === running ? mismatchSeen : { sha256: running, at: now };
  if (now - seen.at < MISMATCH_GRACE_MS) {
    return result('settling', 'none', {}, seen, 'service reports a different hash, waiting for it to restart itself', running);
  }

  const canApiRestart =
    (platform === 'darwin' || platform === 'linux') &&
    Array.isArray(status.features) &&
    status.features.includes(FEATURE_RESTART_V1);
  if (!canApiRestart) {
    return result('outdated', askedMechanism, { surface: true }, seen, 'service cannot be asked to restart', running);
  }

  const attempt = apiRestart && apiRestart.sha256 === running ? apiRestart : null;
  if (attempt === null) {
    if (autoRestarts >= AUTO_RESTART_LIMIT) {
      return surfaceAfterApi(platform, seen, running, 'automatic restarts for this launch are used up');
    }
    return result('outdated', 'api_restart', { automatic: true }, seen, 'restarting the service through its restart command', running);
  }
  if (now - attempt.at < API_RESTART_WINDOW_MS) {
    // The restart was sent a moment ago and its answer is still being awaited.
    return result('outdated', 'api_restart', {}, seen, 'waiting for the restarted service', running);
  }
  return surfaceAfterApi(platform, seen, running, 'service still reports the old hash after a restart');
}

// The restart command was used (or its allowance is spent) and the hash still
// differs. macOS keeps offering the same command: the button refreshes the
// registration and sends it again. Linux falls back to the elevated helper.
function surfaceAfterApi(platform, seen, running, reason) {
  return {
    verdict: 'outdated',
    mechanism: platform === 'darwin' ? 'api_restart' : 'offer_restart',
    automatic: false,
    surface: true,
    running,
    mismatchSeen: seen,
    reason,
  };
}

// ---- macOS: finding and signalling the old service process -----------------
//
// An unprivileged app cannot stop a root LaunchDaemon, and a sidecar from before
// restart_v1 has no command to ask. The one way left is to ask the user for
// administrator rights and send SIGTERM to that process. These two functions
// keep that narrow: the process must be found by the app itself, root-owned and
// running exactly the service's binary, and the elevated command contains
// nothing but constants and the pid.

/**
 * Parses `ps -axo pid=,uid=,comm=` output and returns the pids of root-owned
 * processes whose executable path is exactly binPath. On macOS `comm` is the
 * full path of the executable, spaces included, so everything after the second
 * column is the path.
 * @param {string} psOutput
 * @param {string} binPath
 * @returns {number[]}
 */
function findServicePids(psOutput, binPath) {
  const pids = [];
  for (const line of String(psOutput).split('\n')) {
    const m = /^\s*(\d+)\s+(\d+)\s+(.+?)\s*$/.exec(line);
    if (!m) continue;
    const pid = Number(m[1]);
    if (!Number.isSafeInteger(pid) || pid <= 1) continue;
    if (Number(m[2]) !== 0) continue;
    if (m[3] !== binPath) continue;
    pids.push(pid);
  }
  return pids;
}

/**
 * The AppleScript main.js hands to /usr/bin/osascript to SIGTERM one process
 * with the system's administrator dialog. The shell command is fixed apart from
 * the pid, and it re-checks as root, when the dialog has been answered, that
 * the pid still runs binPath (a pid can be reused while a dialog is open); if
 * not it exits 3 and kills nothing.
 * @param {number} pid
 * @param {string} binPath    a constant of the app
 * @param {string} promptText a constant of the app
 */
function buildAdminTermScript(pid, binPath, promptText) {
  if (!Number.isSafeInteger(pid) || pid <= 1) throw new Error('invalid pid');
  for (const constant of [binPath, promptText]) {
    if (/["'\\\n$`]/.test(constant)) throw new Error('constant cannot be embedded in the script');
  }
  const shell = `case $(/bin/ps -p ${pid} -o comm=) in '${binPath}') /bin/kill -TERM ${pid};; *) exit 3;; esac`;
  return `do shell script "${shell}" with prompt "${promptText}" with administrator privileges`;
}

/**
 * Turns a failed osascript run into the short reason the renderer shows.
 * @param {string} stderr
 */
function describeAdminRestartError(stderr) {
  const text = String(stderr || '');
  if (/\(-128\)|User canceled/i.test(text)) return 'authentication cancelled';
  if (/\(3\)\s*$/m.test(text)) return 'the mesh service changed while the dialog was open; try again';
  return text.trim().split('\n').pop() || 'the administrator restart failed';
}

module.exports = {
  decideMeshServiceUpdate,
  findServicePids,
  buildAdminTermScript,
  describeAdminRestartError,
  MISMATCH_GRACE_MS,
  API_RESTART_WINDOW_MS,
  AUTO_RESTART_LIMIT,
  SELF_RESTART_EXIT_CODE,
  FEATURE_RESTART_V1,
  normaliseHash,
};
