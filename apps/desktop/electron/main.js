'use strict';

const { app, BrowserWindow, shell, ipcMain, dialog, Notification, nativeImage, session } = require('electron');
const { spawn, execFile, execFileSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const http = require('http');
const { pathToFileURL } = require('url');
const { autoUpdater } = require('electron-updater');
const log = require('electron-log');
const { discoverRuntime } = require('./runtime-control');
const { readMemberSession } = require('./member-session');

// ─── App identity ────────────────────────────────────────────────────────────
// Must be set BEFORE app.whenReady() so menu bar / app data path use the right
// name. The macOS Dock *label* in dev still reads "Electron" from the bundled
// Electron.app's Info.plist — that's only fixable in packaged builds (where
// electron-builder writes our productName into the .app bundle).
app.setName('Potluck AI');

// ─── Constants ───────────────────────────────────────────────────────────────

const PYTHON_PORT = 8321;
const SIDECAR_STARTUP_TIMEOUT_MS = 15_000; // 15 seconds
const SIDECAR_POLL_INTERVAL_MS = 500;
const MAX_RESTART_ATTEMPTS = 3;
const SIGKILL_TIMEOUT_MS = 3_000;

// Random shared secret handed to the spawned sidecar (production) and injected
// into every renderer→sidecar request via onBeforeSendHeaders. Stops other local
// processes on the machine from reading the user's memory/chat/tasks/vault over
// 127.0.0.1. Generated fresh each launch. Unused in dev: there the sidecar is
// started by dev:python without a token, so the gate is off and we inject nothing.
let SIDECAR_TOKEN = crypto.randomBytes(32).toString('hex');

// ─── State ───────────────────────────────────────────────────────────────────

let pythonProcess = null;
let attachedRuntime = false;
// A CLI-owned runtime can restart while the app stays open. Re-authenticate the
// new instance before adopting its per-launch credential; never spawn a rival.
const sharedRuntimePoll = setInterval(async () => {
  if (!attachedRuntime || app.isQuitting) return;
  try {
    const record = await discoverRuntime();
    if (record) SIDECAR_TOKEN = record.token;
  } catch (_) { /* The renderer's health checks report runtime unavailability. */ }
}, 3000);
sharedRuntimePoll.unref();
let mainWindow = null;
let restartAttempts = 0;

// The coordinator origin the renderer is currently pointed at (Phase G control
// plane — connectionStore.ts's coordinatorUrl). Runtime-mutable and user-
// overridable, so main can't hardcode it; the renderer pushes it via the
// 'coordinator:set-base' IPC channel (see the handler near the coordinator
// session-auth bridge below) on startup and on every change. Null until the
// renderer's first push. Used ONLY to scope which requests the
// onBeforeSendHeaders injector attaches the member session bearer to — see
// that handler for why the scoping matters.
let coordinatorOrigin = null;

// Deep-link state (magic-link sign-in via the potluck:// scheme).
// pendingDeepLink buffers a URL that arrived before the renderer was ready to
// receive it (cold start, or macOS open-url before the window exists). The
// renderer pulls it once on mount via the 'potluck:get-initial-deep-link' IPC
// channel, which also flips rendererReady so later links are pushed live.
let pendingDeepLink = null;
let rendererReady = false;

// True once the user has consented to an update download (via update:download /
// updater:download). Gates the auto-updater 'error' event: a failed background
// CHECK (feed unreachable, transient network, releases.* hiccup) must stay
// silent — only an error during a user-initiated download/install surfaces a
// banner. A released user must never see "update failed" for something they
// did not do.
let updateDownloadStarted = false;

// app.isQuitting is set in before-quit to stop the crash-restart loop.
app.isQuitting = false;

// ─── Logging setup ───────────────────────────────────────────────────────────

log.transports.file.resolvePathFn = () =>
  path.join(app.getPath('userData'), 'logs', 'main.log');
log.transports.file.level = 'debug';
log.transports.console.level = 'debug';

// ─── Build stamp (staleness detection) ───────────────────────────────────────
//
// The bug this exists to prevent: a build pipeline step (e.g. only rebuilding
// the frontend) can silently leave a WEEK-OLD compiled app installed while
// looking like a fresh build, because nothing on disk told you which git
// commit — or even which day — the running app was actually built from. This
// stamp makes that detectable: the frontend can render a badge, and it's
// logged here on every launch so it's in the log file too.
//
// Packaged: scripts/generate-build-info.js writes build-info.json into this
// same directory at build time (wired into package.json's prepack chain and
// the root Makefile's installer-bundle target) — see that script for exactly
// what it captures. Read once at startup; the file is static for the life of
// a packaged build so there's no reason to re-read it.
//
// Dev (`make desktop`): there is no packaged bundle and no prepack step, so
// compute the same shape live from git. Best-effort only — a dev checkout
// with no .git (unlikely, but not our problem to hard-fail over) degrades to
// sha:"unknown" rather than blocking app startup.
function loadBuildInfo() {
  if (app.isPackaged) {
    try {
      const raw = fs.readFileSync(path.join(__dirname, 'build-info.json'), 'utf8');
      const parsed = JSON.parse(raw);
      return {
        sha: typeof parsed.sha === 'string' ? parsed.sha : 'unknown',
        builtAt: typeof parsed.builtAt === 'string' ? parsed.builtAt : 'unknown',
        packaged: true,
        dirty: !!parsed.dirty,
      };
    } catch (err) {
      log.warn(`[build] Failed to read build-info.json: ${err.message}`);
      return { sha: 'unknown', builtAt: 'unknown', packaged: true, dirty: false };
    }
  }

  try {
    const repoRoot = path.join(__dirname, '..', '..', '..');
    const sha = execFileSync('git', ['rev-parse', '--short', 'HEAD'], {
      cwd: repoRoot,
      encoding: 'utf8',
    }).trim();
    const dirty =
      execFileSync('git', ['status', '--porcelain'], { cwd: repoRoot, encoding: 'utf8' }).trim()
        .length > 0;
    return { sha, builtAt: new Date().toISOString(), packaged: false, dirty };
  } catch (err) {
    log.warn(`[build] Failed to compute dev build info from git: ${err.message}`);
    return { sha: 'dev', builtAt: new Date().toISOString(), packaged: false };
  }
}

const BUILD_INFO = loadBuildInfo();
log.info(
  `[build] sha=${BUILD_INFO.sha} builtAt=${BUILD_INFO.builtAt} packaged=${BUILD_INFO.packaged}`
    + (BUILD_INFO.dirty ? ' dirty=true' : '')
);

// Synchronous by design: preload runs before any page script and assigns this
// straight into the exposed object (see preload.js) so the renderer can read
// window.potluck.app.buildInfo as a plain value with no await — a build
// stamp is static for the process lifetime, so there is nothing to be async
// about. ipcRenderer.sendSync blocks only the preload script, not the page.
// `version` (app.getVersion(), e.g. "0.1.0") rides alongside sha/builtAt so a
// frontend agent can compare it against the server's minimum/latest without a
// second round trip — shape is { version, sha, builtAt, packaged, dirty? }.
ipcMain.on('app:get-build-info-sync', (event) => {
  event.returnValue = { version: app.getVersion(), ...BUILD_INFO };
});

// ─── Deep linking (potluck:// magic-link sign-in) ────────────────────────────

const DEEP_LINK_SCHEME = 'potluck';

/** Find a potluck:// URL in an argv-style array, or null. */
function extractDeepLink(argv) {
  return (
    (argv || []).find(
      (a) => typeof a === 'string' && a.startsWith(`${DEEP_LINK_SCHEME}://`),
    ) || null
  );
}

// The only deep link this app understands today is the magic-link callback.
// Restricting to the exact host/path (not just the scheme) matters: without
// it, any potluck://...?token=... URL — however it arrives (argv on
// Windows/Linux, the 'open-url' OS event on macOS, a cloned bounce page) —
// would reach the renderer's token extraction. See useDeepLinkAuth.ts for the
// matching renderer-side check; this is defense in depth at the routing
// choke point so a future non-auth deep link can't accidentally widen this.
function isAuthCallbackDeepLink(url) {
  try {
    const parsed = new URL(url);
    return (
      parsed.protocol === `${DEEP_LINK_SCHEME}:` &&
      parsed.hostname === 'auth' &&
      parsed.pathname === '/callback'
    );
  } catch {
    return false;
  }
}

/**
 * Deliver a deep link to the renderer, or buffer it until the renderer is ready.
 * Never logs the query string — it carries the one-time magic-link token.
 * Silently drops anything that isn't the exact auth/callback path.
 */
function handleDeepLink(url) {
  if (!url) return;
  if (!isAuthCallbackDeepLink(url)) {
    log.warn(`[deeplink] ignored non-auth-callback deep link: ${url.split('?')[0]}`);
    return;
  }
  log.info(`[deeplink] received ${url.split('?')[0]}`);
  if (rendererReady && mainWindow && mainWindow.webContents) {
    mainWindow.webContents.send('potluck:deep-link', url);
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  } else {
    pendingDeepLink = url;
  }
}

// Register potluck:// as this app's scheme. In dev (defaultApp) the exec path +
// script must be passed so the OS routes the scheme back to this process.
if (process.defaultApp) {
  if (process.argv.length >= 2) {
    app.setAsDefaultProtocolClient(DEEP_LINK_SCHEME, process.execPath, [
      path.resolve(process.argv[1]),
    ]);
  }
} else {
  app.setAsDefaultProtocolClient(DEEP_LINK_SCHEME);
}

// Single-instance lock: on Windows/Linux the OS launches a *second* instance
// carrying the deep-link URL in argv. We forward it to the primary instance and
// let the second quit. Also stops two instances fighting over the sidecar port.
const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
}

app.on('second-instance', (_event, argv) => {
  handleDeepLink(extractDeepLink(argv));
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  }
});

// macOS delivers deep links via open-url, which can fire before the window
// (even before app.whenReady) exists — handleDeepLink buffers in that case.
app.on('open-url', (event, url) => {
  event.preventDefault();
  handleDeepLink(url);
});

// ─── Sidecar path resolution ─────────────────────────────────────────────────

/**
 * Returns the absolute path to the frozen PyInstaller binary in production.
 * Returns null in development (sidecar is started via `pnpm dev:python`).
 *
 * Production layout (set by extraResources in package.json):
 *   macOS/Linux: <app>.app/Contents/Resources/python/potluck-local
 *   Windows:     resources/python/potluck-local.exe
 */
function getSidecarPath() {
  if (!app.isPackaged) {
    return null;
  }
  const binaryName =
    process.platform === 'win32' ? 'potluck-local.exe' : 'potluck-local';
  // onedir layout: potluck-local/<binary> (PyInstaller --onedir)
  return path.join(process.resourcesPath, 'python', 'potluck-local', binaryName);
}

// ─── Sidecar lifecycle ───────────────────────────────────────────────────────

async function startPythonSidecar() {
  const sidecarPath = getSidecarPath();

  if (!sidecarPath) {
    log.info('[sidecar] Dev mode — expecting Python sidecar started via dev:python script.');
    return;
  }

  try {
    const existing = await discoverRuntime();
    if (existing) {
      SIDECAR_TOKEN = existing.token;
      attachedRuntime = true;
      log.info(`[sidecar] Attached to shared runtime PID ${existing.pid}.`);
      return;
    }
  } catch (err) {
    log.error(`[sidecar] ${err.message}`);
    return; // Fail closed; never replace a live runtime's private credential.
  }
  attachedRuntime = false;
  SIDECAR_TOKEN = crypto.randomBytes(32).toString('hex');

  log.info(`[sidecar] Spawning: ${sidecarPath} --port ${PYTHON_PORT}`);

  pythonProcess = spawn(sidecarPath, ['--port', String(PYTHON_PORT)], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {
      ...process.env,
      POTLUCK_PORT: String(PYTHON_PORT),
      POTLUCK_SIDECAR_TOKEN: SIDECAR_TOKEN,
      ...(process.platform !== 'win32' ? { POTLUCK_SHARED_RUNTIME: '1', POTLUCK_RUNTIME_OWNER: 'desktop' } : {}),
    },
  });

  pythonProcess.on('spawn', () => {
    // Reset the crash-loop counter once the process successfully starts.
    // This way only *consecutive* rapid failures count against the limit,
    // not the total lifetime crash count across a long session.
    restartAttempts = 0;
    // A fresh sidecar holds no member session; give it ours once it answers.
    void syncContributorSessionWhenReady();
  });

  pythonProcess.stdout.on('data', (data) => {
    String(data)
      .trim()
      .split('\n')
      .forEach((line) => log.info(`[python] ${line}`));
  });

  pythonProcess.stderr.on('data', (data) => {
    String(data)
      .trim()
      .split('\n')
      .forEach((line) => log.error(`[python] ${line}`));
  });

  pythonProcess.on('exit', (code, signal) => {
    log.warn(`[sidecar] Exited — code=${code} signal=${signal}`);
    pythonProcess = null;

    if (app.isQuitting || attachedRuntime) {
      return; // Expected shutdown, do not restart.
    }

    if (restartAttempts < MAX_RESTART_ATTEMPTS) {
      restartAttempts += 1;
      const delay = Math.min(1000 * restartAttempts, 5000);
      log.warn(
        `[sidecar] Restart attempt ${restartAttempts}/${MAX_RESTART_ATTEMPTS} in ${delay}ms`
      );
      setTimeout(startPythonSidecar, delay);
    } else {
      log.error('[sidecar] Max restart attempts reached. Offering the user a retry.');
      // Previously a dead end: the only button was Quit, so three fast crashes
      // forced the user to kill and relaunch the whole app. Offer an in-place
      // retry instead. It resets the consecutive-crash counter and re-spawns the
      // sidecar without touching the window or the user's session; the renderer's
      // /health poll picks the engine back up and clears the reconnecting banner.
      // Quit stays as the escape hatch.
      dialog
        .showMessageBox({
          type: 'error',
          title: 'Local engine stopped',
          message: 'The local AI engine crashed and could not restart on its own.',
          detail:
            'You can try starting it again, or quit. If this keeps happening, the '
            + 'details are in the log file:\n'
            + path.join(app.getPath('userData'), 'logs', 'main.log'),
          buttons: ['Try again', 'Quit'],
          defaultId: 0,
          cancelId: 1,
        })
        .then(({ response }) => {
          if (response === 0) {
            log.info('[sidecar] User chose retry; resetting crash counter and restarting.');
            restartAttempts = 0;
            startPythonSidecar();
          } else {
            app.quit();
          }
        });
    }
  });

  pythonProcess.on('error', (err) => {
    log.error(`[sidecar] Failed to spawn: ${err.message}`);
  });
}

// ─── Health check ────────────────────────────────────────────────────────────

/**
 * Polls GET /health on the sidecar until it returns HTTP 200 or the timeout
 * elapses. Returns true on success, false on timeout.
 */
async function waitForSidecar() {
  const deadline = Date.now() + SIDECAR_STARTUP_TIMEOUT_MS;
  let attempt = 0;

  while (Date.now() < deadline) {
    attempt += 1;
    try {
      if (app.isPackaged && process.platform !== 'win32') {
        const record = await discoverRuntime();
        if (!record) throw new Error('Shared runtime not ready');
        SIDECAR_TOKEN = record.token;
        attachedRuntime = record.pid !== pythonProcess?.pid;
      }
      const res = await fetch(`http://127.0.0.1:${PYTHON_PORT}/health`, { signal: AbortSignal.timeout(1000) });
      if (res.ok) {
        log.info(`[sidecar] Health check passed after ${attempt} attempt(s).`);
        return true;
      }
      log.debug(`[sidecar] Health check attempt ${attempt}: HTTP ${res.status}`);
    } catch (err) {
      log.debug(`[sidecar] Health check attempt ${attempt}: ${err.message}`);
    }

    await new Promise((r) => setTimeout(r, SIDECAR_POLL_INTERVAL_MS));
  }

  log.error('[sidecar] Timed out waiting for sidecar health check.');
  return false;
}

// ─── Navigation & external-link hardening ────────────────────────────────────
//
// Two independent gates apply to every URL the renderer tries to leave the
// current page for:
//
//   1. In-window navigation (will-navigate — e.g. a clicked <a href> with no
//      target) is allowed ONLY to the app's own origin: the exact packaged
//      index.html in production, or the exact Vite dev-server origin in dev.
//      There is deliberately no "local passthrough" exception for file:// or
//      localhost/127.0.0.1 — a poisoned memory/chat markdown link
//      (`[x](file:///…)`) must not be able to replace the whole window (and
//      its live preload contextBridge — window.potluck, carrying the cloud
//      bearer session + mesh access) with arbitrary local content. Everything
//      else is denied in-window and, if it's an allowed external scheme,
//      handed to the OS browser via safeOpenExternal instead.
//   2. shell.openExternal() only ever receives http:/https: URLs. Anything
//      else (file:, javascript:, custom schemes, UNC paths) is rejected and
//      logged — openExternal on an arbitrary scheme can trigger OS-level
//      file/UNC handling outside our control.

/**
 * The one URL in-window navigation is allowed to land on: our own app shell.
 * Recomputed (not cached) so it always reflects the current app.isPackaged
 * state, which is fixed for the process lifetime but keeps this testable.
 */
function getExpectedAppURL() {
  if (app.isPackaged) {
    return pathToFileURL(
      path.join(process.resourcesPath, 'frontend', 'index.html')
    ).href;
  }
  return 'http://localhost:5173/';
}

/**
 * True only if targetURL is our own app shell (protocol + host + pathname
 * match exactly). Hash/search are ignored: the frontend uses HashRouter
 * specifically because the packaged app is file:// (see frontend/src/main.tsx),
 * so route changes live in the fragment and never reach will-navigate as a
 * top-level navigation (same-document hash changes fire did-navigate-in-page
 * instead) — but we don't rely on that here, we just don't gate on it.
 */
function isAppOrigin(targetURL) {
  let expected;
  let target;
  try {
    expected = new URL(getExpectedAppURL());
    target = new URL(targetURL);
  } catch (_) {
    return false;
  }
  return (
    target.protocol === expected.protocol &&
    target.host === expected.host &&
    target.pathname === expected.pathname
  );
}

/**
 * The only path anything (renderer-triggered window.open, or a denied
 * will-navigate) may reach the OS shell through. Allowlists url.protocol to
 * exactly http:/https:; everything else — file:, javascript:, custom
 * schemes, UNC paths — is rejected and logged, never handed to
 * shell.openExternal.
 */
function safeOpenExternal(rawURL) {
  let parsed;
  try {
    parsed = new URL(rawURL);
  } catch (_) {
    log.warn(`[nav] Rejected openExternal — unparseable URL.`);
    return;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    log.warn(`[nav] Rejected openExternal — disallowed scheme: ${parsed.protocol}`);
    return;
  }
  shell.openExternal(rawURL);
}

// ─── BrowserWindow ───────────────────────────────────────────────────────────

async function createWindow() {
  const isMac = process.platform === 'darwin';

  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    // hiddenInset gives the native traffic-light buttons with a clean look on Mac.
    // On Windows/Linux we use the standard OS frame.
    titleBarStyle: isMac ? 'hiddenInset' : 'default',
    frame: !isMac,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
    // Icon path — the file is expected at apps/desktop/electron/icons/icon.png.
    // If it does not exist yet, Electron will fall back to the default icon.
    icon: path.join(__dirname, 'icons', 'icon.png'),
    show: false, // Prevent white flash — show after ready-to-show.
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  // Inject auth into renderer→{sidecar,coordinator} requests.
  //
  // session.defaultSession.webRequest.onBeforeSendHeaders is a SINGLE-listener
  // setter — calling it twice REPLACES the first registration. So this one
  // handler does both jobs, branching on the request's origin, with an
  // independent packaged-only guard on the sidecar branch only:
  //   - sidecar origin (127.0.0.1/localhost:PYTHON_PORT) -> SIDECAR_TOKEN
  //     bearer, PACKAGED ONLY. In dev the sidecar is started via `pnpm
  //     dev:python` with no token (its own auth gate is off), so injecting
  //     one there would be inert at best; the guard keeps that explicit
  //     rather than relying on it being a no-op.
  //   - the currently-configured coordinator origin (pushed from the
  //     renderer over 'coordinator:set-base' — see the handler below, and
  //     connectionStore.ts for where the renderer tracks it) -> the member
  //     session bearer, so SSE requests like /jobs/stream (which cannot go
  //     through the buffering coordinator:session-request bridge — that
  //     reads the whole body with res.text(), which does not work for a
  //     live stream) still carry a session. Runs in BOTH dev and packaged:
  //     unlike the sidecar, dev's coordinator calls hit a real coordinator
  //     (local or hosted) that enforces the same session auth prod does, so
  //     the dev app's network-chat streaming needs the same bearer prod
  //     gets. No coordinator origin known yet, or the token isn't loaded ->
  //     inject nothing for it.
  //   - anything else -> untouched. This scoping is a security boundary: the
  //     session bearer must only ever go to the exact coordinator origin the
  //     user configured, never broadcast to arbitrary hosts.
  // The URL filter is widened from sidecar-only to all http/https so the
  // coordinator branch is even seen; that's fine because the else-branch
  // above forwards non-matching requests' headers unmodified. Registered
  // unconditionally (not gated on app.isPackaged) so the coordinator branch
  // is live in dev too — the sidecar branch's own app.isPackaged check below
  // keeps that half prod-only.
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: ['http://*/*', 'https://*/*'] },
    (details, callback) => {
      let origin;
      try {
        origin = new URL(details.url).origin;
      } catch (_) {
        callback({ requestHeaders: details.requestHeaders });
        return;
      }
      if (
        app.isPackaged
        && (origin === `http://127.0.0.1:${PYTHON_PORT}` || origin === `http://localhost:${PYTHON_PORT}`)
      ) {
        details.requestHeaders['Authorization'] = `Bearer ${SIDECAR_TOKEN}`;
      } else if (coordinatorOrigin && origin === coordinatorOrigin) {
        const sessionToken = loadCloudToken();
        if (sessionToken) {
          details.requestHeaders['Authorization'] = `Bearer ${sessionToken}`;
        }
        details.requestHeaders['X-Potluck-Client-Version'] = app.getVersion();
      }
      callback({ requestHeaders: details.requestHeaders });
    }
  );

  if (app.isPackaged) {
    // Content-Security-Policy — defence-in-depth for the renderer. We already run
    // sandbox:true + contextIsolation, load only local file:// content, and send
    // external navigations to the OS browser, so a renderer compromise has a small
    // blast radius. The CSP closes the last gap: it stops any script that DID slip
    // in (a supply-chain-tainted dependency, or an injection through the markdown /
    // syntax-highlight render path) from loading remote code or executing as an
    // inline <script>.
    //
    // Packaged (file://) builds only. Dev loads from the Vite server
    // (http://localhost:5173) whose HMR needs inline scripts, eval, and a
    // websocket; forcing a strict policy there would break the dev loop. Same
    // app.isPackaged reasoning as the token injector above.
    //
    // Verified safe against the shipped bundle before enabling: dist/index.html has
    // no inline <script> (one external ./assets/*.js), and the bundle has no eval()/
    // new Function()/runtime WebAssembly/web-workers — so script-src 'self' with no
    // 'unsafe-eval' or 'wasm-unsafe-eval' loads the app cleanly. Re-verify after any
    // dependency bump that could introduce those (re-run the static scan).
    //
    // connect-src is deliberately wide (any http/https/ws host). The coordinator
    // URL is user-editable to any LAN/mesh IP (frontend connectionStore.ts) and is
    // fetched directly by the renderer, so a fixed origin allowlist is impossible.
    // That is an acceptable v1 posture because script-src 'self' already prevents
    // injected code from RUNNING to abuse the open connect-src.
    // TODO(v0.5): once the coordinator is reached only via the sidecar (or its host
    // set is pinned), tighten connect-src to explicit origins.
    const CSP = [
      "default-src 'self'",
      "script-src 'self'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data: blob:",
      "font-src 'self' data:",
      "connect-src 'self' http: https: ws: wss:",
      "worker-src 'self' blob:",
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
    ].join('; ');

    session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
      // Stamp only the top-level document response — its policy then governs every
      // subresource and fetch the page makes. Leave API/asset responses untouched
      // (a CSP header on a non-document response is ignored anyway). We must always
      // call callback, or every response in the session stalls.
      if (details.resourceType !== 'mainFrame') {
        callback({ responseHeaders: details.responseHeaders });
        return;
      }
      callback({
        responseHeaders: {
          ...(details.responseHeaders || {}),
          'Content-Security-Policy': [CSP],
        },
      });
    });
  }

  // Route all window.open() / anchor target="_blank" clicks to the system
  // browser, through the http(s)-only allowlist (see safeOpenExternal above).
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    safeOpenExternal(url);
    return { action: 'deny' };
  });

  // Gate ALL in-window navigation to the app's own origin (see isAppOrigin
  // above). Anything else — including file:// and localhost/127.0.0.1, which
  // used to pass through unchecked — is denied in-window and, if it's an
  // allowed external scheme, sent to the OS browser instead.
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (isAppOrigin(url)) {
      return; // Our own app shell — allow (e.g. a full reload).
    }
    event.preventDefault();
    safeOpenExternal(url);
  });

  if (!app.isPackaged) {
    mainWindow.loadURL('http://localhost:5173');
    // Skip DevTools when POTLUCK_DISABLE_DEVTOOLS is set (demo-video capture
    // sets it) — a detached DevTools window confuses Playwright's recordVideo
    // and gets filmed instead of the app. Normal `make desktop` is unaffected.
    if (!process.env.POTLUCK_DISABLE_DEVTOOLS) {
      mainWindow.webContents.openDevTools({ mode: 'detach' });
    }
  } else {
    mainWindow.loadFile(
      path.join(process.resourcesPath, 'frontend', 'index.html')
    );
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

// ─── IPC handlers ────────────────────────────────────────────────────────────

ipcMain.handle('select-directory', async (_event) => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory'],
  });
  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }
  return result.filePaths[0];
});

ipcMain.handle('show-notification', (_event, title, body) => {
  if (Notification.isSupported()) {
    const notif = new Notification({ title: String(title), body: String(body) });
    // Electron 42+ uses UNNotification on macOS: unsigned/ad-hoc dev builds
    // can't post and emit 'failed' instead of throwing. Log it so dev failures
    // aren't silent (production is signed + notarized, so this won't fire there).
    notif.on('failed', (_e, error) => log.warn(`[notification] failed: ${error}`));
    notif.show();
  }
});

ipcMain.handle('get-app-version', () => app.getVersion());

// True only when this build can actually self-update: it must be packaged AND
// electron-builder must have emitted app-update.yml (the publish-feed config).
// A `--dir` / dogfood / unsigned local build is packaged but omits that file,
// so calling checkForUpdates/downloadUpdate would throw
// "ENOENT ... app-update.yml" and surface a bogus "update failed" banner.
// Treat those builds as "updates disabled" and no-op everywhere.
function isUpdaterConfigured() {
  return (
    app.isPackaged &&
    fs.existsSync(path.join(process.resourcesPath, 'app-update.yml'))
  );
}

// ─── Auto-updater IPC (renderer-driven download + install) ───────────────────
// autoDownload is off, so the renderer decides when to pull the update (banner
// "Download") and when to restart into it ("Restart"). These are registered
// unconditionally so the facade never rejects; a build with no update feed
// (dev / dir / unsigned) returns a benign { status: 'unavailable' }.
ipcMain.handle('updater:download', async () => {
  if (!isUpdaterConfigured()) return { status: 'unavailable' };
  log.info('[updater] Renderer requested download.');
  updateDownloadStarted = true;
  return autoUpdater.downloadUpdate();
});

ipcMain.handle('updater:quit-and-install', () => {
  if (!isUpdaterConfigured()) return { status: 'unavailable' };
  log.info('[updater] Renderer requested restart-to-install.');
  // Defer so the IPC reply flushes before the app tears itself down.
  setImmediate(() => autoUpdater.quitAndInstall());
});

// update:check / update:download / update:install — same three actions as
// updater:download / updater:quit-and-install above, added under the
// update:* names for the updater-namespaced preload API (window.potluck.
// updater). Kept alongside the older channels rather than renaming them, so
// nothing already wired to updater:download / updater:quit-and-install
// breaks. All three no-op with a benign { status: 'unavailable' } when this
// build has no update feed (isUpdaterConfigured() false): electron-updater
// throws "ENOENT ... app-update.yml" if you call checkForUpdates/
// downloadUpdate/quitAndInstall on a dev / --dir / unsigned build.
ipcMain.handle('update:check', async () => {
  if (!isUpdaterConfigured()) return { status: 'unavailable' };
  log.info('[updater] Renderer requested a manual update check.');
  try {
    await autoUpdater.checkForUpdates();
    return { status: 'ok' };
  } catch (err) {
    log.error(`[updater] Manual check failed: ${err.message}`);
    return { status: 'error', message: err.message };
  }
});

ipcMain.handle('update:download', async () => {
  if (!isUpdaterConfigured()) return { status: 'unavailable' };
  log.info('[updater] Renderer requested download.');
  updateDownloadStarted = true;
  try {
    await autoUpdater.downloadUpdate();
    return { status: 'ok' };
  } catch (err) {
    log.error(`[updater] Download failed: ${err.message}`);
    return { status: 'error', message: err.message };
  }
});

ipcMain.handle('update:install', () => {
  if (!isUpdaterConfigured()) return { status: 'unavailable' };
  log.info('[updater] Renderer requested restart-to-install.');
  // Defer so the IPC reply flushes before the app tears itself down.
  setImmediate(() => autoUpdater.quitAndInstall());
  return { status: 'ok' };
});

// Renderer pulls any deep link that arrived before it was ready (cold start).
// The first call also marks the renderer ready, so later links are pushed live
// over the 'potluck:deep-link' channel instead of being buffered.
ipcMain.handle('potluck:get-initial-deep-link', () => {
  rendererReady = true;
  const url = pendingDeepLink;
  pendingDeepLink = null;
  return url;
});

// ─── Mesh IPC bridge ─────────────────────────────────────────────────────────
//
// The Go mesh sidecar exposes an HTTP server over a Unix domain socket
// (macOS/Linux) or named pipe (Windows). See docs/phase-g/local-ipc.md.
// Renderer code can't dial Unix sockets directly, so main.js proxies the
// HTTP request via ipcMain.handle('mesh:request').
//
// Status / peers / pair / unpair endpoints — all small JSON request/response
// pairs. We deliberately do NOT forward the WS /events stream here: per the
// shipped /agents-view precedent, polling at 2–5s is cheaper to package and
// keeps the architecture simple. If live push becomes necessary, add a
// separate `mesh:subscribe-events` channel.
//
// Errors come in three flavours:
//   * `unreachable`  — the sidecar isn't running / socket file doesn't exist
//   * `timeout`      — the sidecar accepted but didn't reply in 10s
//   * regular HTTP   — status >= 400 with a JSON `{ error: { code, message } }`
//                      body (the localapi server's contract).

const MESH_REQUEST_TIMEOUT_MS = 10_000;

function meshSocketPath() {
  if (process.platform === 'win32') {
    return '\\\\.\\pipe\\potluck-mesh';
  }
  if (process.platform === 'darwin') {
    // macOS's launchd / RUNNING_FOR_USER semantics make XDG_RUNTIME_DIR
    // unreliable. The Go sidecar's defaultSocketPath uses /tmp on Mac.
    return '/tmp/potluck-mesh.sock';
  }
  // Linux: prefer the fixed system-wide path the app-driven root systemd
  // unit (packaging/linux/mesh-install-helper.sh) and the planned .deb/.rpm
  // unattended unit (packaging/daemons/potluck-mesh.service) both pin via
  // POTLUCK_LOCAL_API_SOCKET=/run/potluck-mesh.sock. A per-user
  // XDG_RUNTIME_DIR/$UID path doesn't work for a system-level daemon: it
  // may start before first login (the dir doesn't exist yet) and gets torn
  // down + recreated on every relogin (the socket file would vanish even
  // though the daemon is still running) — see the header comment in
  // mesh-install-helper.sh for the full reasoning. Fall back to the
  // per-user XDG path only when the fixed path isn't present, which covers
  // a manually-run dev binary (`go run ./cmd/potluck-mesh`, no root, no
  // systemd) using the Go binary's own defaultSocketPath().
  const FIXED_LINUX_SOCKET_PATH = '/run/potluck-mesh.sock';
  if (fs.existsSync(FIXED_LINUX_SOCKET_PATH)) {
    return FIXED_LINUX_SOCKET_PATH;
  }
  const xdg = process.env.XDG_RUNTIME_DIR;
  if (xdg && xdg.trim().length > 0) {
    return path.join(xdg, 'potluck-mesh.sock');
  }
  const uid = typeof process.getuid === 'function' ? process.getuid() : 1000;
  return `/run/user/${uid}/potluck-mesh.sock`;
}

function meshRequest({ method, path: reqPath, body }) {
  return new Promise((resolve, reject) => {
    const opts = {
      socketPath: meshSocketPath(),
      method,
      path: reqPath,
      headers: { Accept: 'application/json' },
    };

    let bodyBuf = null;
    if (body !== undefined && body !== null) {
      bodyBuf = Buffer.from(JSON.stringify(body));
      opts.headers['Content-Type'] = 'application/json';
      opts.headers['Content-Length'] = bodyBuf.length;
    }

    const req = http.request(opts, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const buf = Buffer.concat(chunks);
        const text = buf.toString('utf8');
        let parsed = null;
        if (text.length > 0) {
          try {
            parsed = JSON.parse(text);
          } catch (_) {
            parsed = { raw: text };
          }
        }
        resolve({ status: res.statusCode, body: parsed });
      });
    });

    req.setTimeout(MESH_REQUEST_TIMEOUT_MS, () => {
      req.destroy(new Error('timeout'));
    });

    req.on('error', (err) => {
      // ENOENT / ECONNREFUSED → sidecar isn't running. Surface a stable
      // error code so the renderer can render the "Mesh sidecar offline"
      // state without parsing OS error strings.
      const code = err && err.code ? err.code : '';
      if (code === 'ENOENT' || code === 'ECONNREFUSED') {
        reject(Object.assign(new Error('sidecar_unreachable'), { code: 'sidecar_unreachable' }));
        return;
      }
      if (err && err.message === 'timeout') {
        reject(Object.assign(new Error('timeout'), { code: 'timeout' }));
        return;
      }
      reject(err);
    });

    if (bodyBuf) req.write(bodyBuf);
    req.end();
  });
}

ipcMain.handle('mesh:request', async (_evt, method, reqPath, body) => {
  // Whitelist methods to keep the surface tight. Renderer must only call
  // the methods we expose; an arbitrary method through the bridge would
  // be a bug, not a feature.
  const allowed = new Set(['GET', 'POST', 'DELETE']);
  if (typeof method !== 'string' || !allowed.has(method)) {
    return { status: 400, body: { error: { code: 'bad_method', message: 'method not allowed' } } };
  }
  if (typeof reqPath !== 'string' || !reqPath.startsWith('/')) {
    return { status: 400, body: { error: { code: 'bad_path', message: 'path must start with /' } } };
  }
  try {
    return await meshRequest({ method, path: reqPath, body });
  } catch (err) {
    const code = err && err.code ? err.code : 'unknown';
    return {
      status: 0,
      body: { error: { code, message: err && err.message ? err.message : String(err) } },
    };
  }
});

// ─── Mesh daemon supervision (macOS: SMAppService root LaunchDaemon) ─────────
//
// This is the "app brings up the mesh" layer, distinct from the mesh:request
// bridge above (which only talks to an ALREADY-running sidecar over its
// socket). The mesh binary needs root for the utun device, so on macOS it
// runs as a root LaunchDaemon registered via SMAppService — never via `sudo`,
// never hand-run by the user. main.js never spawns/kills this daemon itself
// (unlike the Python sidecar above): it is installed once, OUTLIVES the
// Electron app across quits/relaunches/updates, and is only driven through
// the signed mesh-helper Swift CLI (native/mesh-helper/), which wraps
// SMAppService.daemon(plistName:) — see that file's header comment for why
// it must be a sibling of the main executable at Contents/MacOS/mesh-helper
// (electron-builder's mac.extraFiles in package.json), not Resources/.
//
// The static LaunchDaemon plist (packaging/daemons/ai.trypotluck.mesh.plist,
// embedded the same way at Contents/Library/LaunchDaemons/) is baked in at
// build time and can't be rewritten post-install — see its header comment.
//
// Cross-platform shape: the renderer-facing contract (channel names + the
// `state` enum) is the same on every OS so the frontend can be wired once.
// Only macOS is implemented; Linux/Windows always resolve state:'unsupported'
// with a TODO for now (systemd/pkexec on Linux, a Windows Service on
// Windows — neither needs root/UAC-avoidance tricks the way macOS does, so
// their mechanism will look different, not just a port of this one).
//
// State enum (superset of what the task spec listed — 'starting' added
// below because SMAppService's 'enabled' means "registered + approved," NOT
// "the socket is actually answering yet," and collapsing that into one of
// the other four buckets would be actively misleading to the UI):
//   'not_installed'    — never registered (or SMAppService lost track of it)
//   'requires_approval'— registered, but the user must flip it on in
//                        System Settings > Login Items & Extensions
//   'starting'         — SMAppService says enabled, but GET /status on the
//                        daemon's own IPC socket hasn't answered yet
//                        (freshly registered, or the daemon is unhealthy)
//   'running'          — enabled AND the socket answered 200 on /status
//   'unsupported'      — this platform's mechanism isn't implemented yet

/**
 * Absolute path to the signed mesh-helper CLI inside the packaged app, or
 * null in dev (there is no packaged bundle to resolve the sibling path
 * against, and no point registering a real system LaunchDaemon from a dev
 * checkout). Mirrors getSidecarPath()'s app.isPackaged guard above.
 *
 * process.resourcesPath is Contents/Resources; the helper lives at the
 * sibling Contents/MacOS/mesh-helper (see the big comment block above).
 */
function getMeshHelperPath() {
  if (!app.isPackaged || process.platform !== 'darwin') {
    return null;
  }
  return path.join(process.resourcesPath, '..', 'MacOS', 'mesh-helper');
}

const MESH_HELPER_TIMEOUT_MS = 15_000;

/**
 * Runs the mesh-helper CLI with a single subcommand and captures stdout.
 * Never rejects — callers get { ok: false, error } instead, since every
 * caller needs to turn a helper failure into a `state` value, not an
 * exception.
 */
function runMeshHelper(subcommand) {
  return new Promise((resolve) => {
    const helperPath = getMeshHelperPath();
    if (!helperPath) {
      resolve({ ok: false, error: 'mesh-helper unavailable (dev mode or non-macOS)' });
      return;
    }
    execFile(helperPath, [subcommand], { timeout: MESH_HELPER_TIMEOUT_MS }, (err, stdout, stderr) => {
      if (err) {
        const detail = (stderr && stderr.trim()) || err.message;
        log.warn(`[mesh-daemon] helper '${subcommand}' failed: ${detail}`);
        resolve({ ok: false, error: detail });
        return;
      }
      resolve({ ok: true, output: stdout.trim() });
    });
  });
}

/**
 * Computes the full macOS daemon status: shells out to the helper's `status`
 * subcommand for the SMAppService.Status, then — only when that reports
 * 'enabled' — probes the daemon's own IPC socket (GET /status, via the
 * existing meshRequest() above) to confirm it's actually up, not just
 * registered. See the state-enum comment above for what each value means.
 */
async function computeMacDaemonStatus() {
  const helperResult = await runMeshHelper('status');
  if (!helperResult.ok) {
    return { platform: 'darwin', state: 'not_installed', detail: helperResult.error };
  }

  const token = helperResult.output;
  if (token === 'requiresApproval') {
    return { platform: 'darwin', state: 'requires_approval' };
  }
  if (token === 'notRegistered' || token === 'notFound') {
    return { platform: 'darwin', state: 'not_installed' };
  }
  if (token === 'enabled') {
    try {
      const res = await meshRequest({ method: 'GET', path: '/status' });
      if (res.status === 200) {
        return { platform: 'darwin', state: 'running' };
      }
      return { platform: 'darwin', state: 'starting', detail: `socket responded with HTTP ${res.status}` };
    } catch (err) {
      const code = err && err.code ? err.code : (err && err.message) || 'socket unreachable';
      return { platform: 'darwin', state: 'starting', detail: code };
    }
  }
  // Unrecognised token — a future SMAppService.Status case the helper
  // doesn't know how to map (see statusToken() in main.swift). Fail safe
  // toward "not installed" rather than silently claiming success.
  return { platform: 'darwin', state: 'not_installed', detail: `unrecognised helper status: ${token}` };
}

// ─── Mesh daemon supervision (Linux: root systemd unit via polkit/pkexec) ───
//
// Mirrors the macOS section above, but there is no SMAppService-equivalent
// "register once, OS handles consent + persistence" API on Linux — systemd
// has no built-in user-consent step, so THIS layer has to (a) drive a
// polkit/pkexec authorization prompt itself and (b) poll systemd directly
// for unit state (there's no separate "requires_approval" step the way
// SMAppService has: pkexec's auth dialog is synchronous — either the user
// authenticates and the install proceeds, or it doesn't happen at all, so
// Linux never reports 'requires_approval'; a cancelled/failed pkexec prompt
// just reports 'not_installed' with a detail string, same shape as macOS's
// runMeshHelper failure path).
//
// The privileged half (rendering + installing + starting the systemd unit)
// lives entirely in packaging/linux/mesh-install-helper.sh, invoked via
// pkexec against a FIXED path with a FIXED, whitelisted argument list — see
// that script's header comment for the full security model and
// packaging/linux/polkit/ai.trypotluck.mesh.policy for the polkit action
// that authorizes it. main.js never constructs a shell command from
// renderer input for this: mesh:enable takes no arguments from the
// renderer at all (see ipcMain.handle('mesh:enable') below) — every value
// passed to pkexec here is a fixed constant or a value main.js itself
// computed (its own uid/gid), never renderer-supplied.
//
// Querying unit state (systemctl is-active/is-enabled) needs no privilege,
// so computeLinuxDaemonStatus() never touches pkexec — only mesh:enable
// (the install path) does.

/**
 * Absolute path to the polkit-authorized install helper. In a packaged
 * .deb/.rpm install this is /usr/lib/potluck/mesh-install-helper.sh (must
 * match the path baked into ai.trypotluck.mesh.policy's
 * org.freedesktop.policykit.exec.path annotation — pkexec will refuse to
 * run anything else with this action's branded prompt). In dev (unpackaged,
 * app.isPackaged === false) there is no installed helper, so this returns
 * null and mesh:enable reports 'not_installed' with an explanatory detail —
 * same "no dev-mode daemon install" posture as getMeshHelperPath() on
 * macOS.
 */
function getLinuxMeshHelperPath() {
  if (!app.isPackaged || process.platform !== 'linux') {
    return null;
  }
  return '/usr/lib/potluck/mesh-install-helper.sh';
}

/**
 * Absolute path to the mesh binary this install should point the systemd
 * unit's ExecStart at. Must land inside mesh-install-helper.sh's
 * ALLOWED_MESH_BIN_PREFIXES allowlist — see that script's validate_mesh_bin()
 * — or the privileged side will refuse it. Mirrors getSidecarPath()'s
 * resourcesPath resolution for the Python sidecar elsewhere in this file.
 */
function getLinuxMeshBinPath() {
  if (!app.isPackaged) return null;
  return path.join(process.resourcesPath, 'mesh', 'potluck-mesh');
}

const LINUX_MESH_INSTALL_TIMEOUT_MS = 30_000;

// Production defaults — same values baked into the macOS LaunchDaemon plist
// (packaging/daemons/ai.trypotluck.mesh.plist). A self-hosted/LAN
// coordinator override goes through the runtime-settings PATCH /settings
// mechanism after install, same as macOS; this is only the bootstrap value.
const LINUX_MESH_COORDINATOR_URL_DEFAULT = 'https://coordinator.trypotluck.ai';
const LINUX_MESH_DERP_URL_DEFAULT = 'tcp+tls://derp.trypotluck.ai:443';

/**
 * Runs the install helper via pkexec with a fixed subcommand + fixed
 * argument shape. Never rejects — same contract as runMeshHelper() above,
 * for the same reason (every caller needs a `state`, not an exception).
 */
function runLinuxMeshHelper(args) {
  return new Promise((resolve) => {
    const helperPath = getLinuxMeshHelperPath();
    if (!helperPath) {
      resolve({ ok: false, error: 'mesh-install-helper unavailable (dev mode or non-Linux)' });
      return;
    }
    const pkexecBin = '/usr/bin/pkexec';
    if (!fs.existsSync(pkexecBin)) {
      resolve({ ok: false, error: 'pkexec not found — polkit is required for mesh install on Linux' });
      return;
    }
    execFile(
      pkexecBin,
      [helperPath, ...args],
      { timeout: LINUX_MESH_INSTALL_TIMEOUT_MS },
      (err, stdout, stderr) => {
        if (err) {
          // pkexec exit code 126 = auth dialog dismissed/declined, 127 = not
          // authorized. Distinguish "user said no" from "the helper itself
          // failed" in the detail string so the UI could tell them apart
          // later even though both map to the same not_installed state today.
          const reason =
            err.code === 126
              ? 'authentication cancelled'
              : err.code === 127
                ? 'not authorized'
                : (stderr && stderr.trim()) || err.message;
          log.warn(`[mesh-daemon-linux] pkexec helper failed: ${reason}`);
          resolve({ ok: false, error: reason });
          return;
        }
        resolve({ ok: true, output: stdout.trim() });
      },
    );
  });
}

/**
 * Computes Linux daemon status by asking systemd directly (no privilege
 * needed to read unit state) and, when active, probing the local IPC
 * socket exactly like computeMacDaemonStatus() does. Uses `systemctl
 * is-active` rather than parsing `systemctl status` output, which is not a
 * stable machine-readable format.
 */
function systemctlIsActive(unitName) {
  return new Promise((resolve) => {
    execFile('systemctl', ['is-active', unitName], { timeout: 5_000 }, (err, stdout) => {
      // is-active exits non-zero for every state except "active" — that is
      // normal/expected here (e.g. "inactive", "failed", "unknown" for a
      // never-installed unit), not a failure to be logged.
      resolve(stdout.trim());
    });
  });
}

function unitFileExists(unitName) {
  return new Promise((resolve) => {
    execFile('systemctl', ['list-unit-files', unitName], { timeout: 5_000 }, (err, stdout) => {
      if (err) {
        resolve(false);
        return;
      }
      resolve(stdout.includes(unitName));
    });
  });
}

// Reads a few machine-readable properties of a unit so we can tell a genuine
// crash / crash-loop apart from a transient startup. NRestarts climbs when
// systemd's Restart=on-failure keeps relaunching a unit that immediately dies
// (e.g. a bad ExecStart) — the classic "stuck forever" case a user would
// otherwise see as an endless "Starting…".
function systemctlShow(unitName) {
  return new Promise((resolve) => {
    execFile(
      'systemctl',
      ['show', unitName, '-p', 'ActiveState', '-p', 'SubState', '-p', 'NRestarts', '-p', 'Result'],
      { timeout: 5_000 },
      (err, stdout) => {
        const props = {};
        String(stdout || '')
          .split('\n')
          .forEach((line) => {
            const eq = line.indexOf('=');
            if (eq > 0) props[line.slice(0, eq)] = line.slice(eq + 1).trim();
          });
        resolve(props);
      },
    );
  });
}

async function computeLinuxDaemonStatus() {
  const exists = await unitFileExists('potluck-mesh.service');
  if (!exists) {
    return { platform: 'linux', state: 'not_installed' };
  }
  const activeState = await systemctlIsActive('potluck-mesh.service');
  if (activeState !== 'active') {
    // Distinguish a real failure / crash-loop from a transient startup, so the
    // UI can offer "Retry" instead of spinning on "Starting…" indefinitely.
    const props = await systemctlShow('potluck-mesh.service');
    const restarts = Number(props.NRestarts || 0);
    const failed =
      activeState === 'failed' ||
      props.ActiveState === 'failed' ||
      props.SubState === 'failed' ||
      (props.Result && props.Result !== 'success' && props.Result !== 'unset') ||
      restarts >= 3; // auto-restart loop = effectively failed
    if (failed) {
      return {
        platform: 'linux',
        state: 'failed',
        detail: `service ${props.ActiveState || activeState}/${props.SubState || '?'}${
          restarts ? `, ${restarts} restart attempts` : ''
        }`,
      };
    }
    return { platform: 'linux', state: 'starting', detail: `systemd unit state: ${activeState}` };
  }
  try {
    const res = await meshRequest({ method: 'GET', path: '/status' });
    if (res.status === 200) {
      return { platform: 'linux', state: 'running' };
    }
    return { platform: 'linux', state: 'starting', detail: `socket responded with HTTP ${res.status}` };
  } catch (err) {
    const code = err && err.code ? err.code : (err && err.message) || 'socket unreachable';
    return { platform: 'linux', state: 'starting', detail: code };
  }
}

// ─── Mesh daemon supervision (Windows: SYSTEM Scheduled Task via UAC) ───────
//
// Mirrors the macOS/Linux sections above. Windows has neither an
// SMAppService equivalent nor (for this binary) a real SCM-compliant
// Windows Service — potluck-mesh.exe is a plain console binary and does not
// implement the Service Control protocol (StartServiceCtrlDispatcher), so
// `sc.exe start` on it fails after ~30s ("Cannot start service"). Retrofitting
// that is a real Go change (golang.org/x/sys/windows/svc), out of scope
// here. Instead this drives a SYSTEM-run, "At startup" + "run with highest
// privileges" Scheduled Task — Task Scheduler just launches the .exe
// directly (no SCM handshake needed), which the existing binary already
// satisfies, and covers "needs admin for the Wintun adapter" + "persist
// across reboots" + "restart on crash" the same way the macOS LaunchDaemon
// and Linux systemd unit do. Verified end-to-end on a real Windows box: the
// installed task runs the daemon as NT AUTHORITY\SYSTEM and the pipe (see
// meshSocketPath() above and internal/localapi/listener_windows.go's DACL
// fix) is reachable from the interactively logged-in desktop user.
//
// The privileged half (copying the binary + wintun.dll into an admin-owned
// directory, and installing/starting the Scheduled Task) lives entirely in
// packaging/windows/mesh-install-helper.ps1, elevated via a single
// Start-Process -Verb RunAs prompt (the Windows equivalent of ShellExecute
// 'runas') against a FIXED script path with a FIXED argument shape — see
// that script's header comment for the full security model. As on Linux,
// main.js never constructs anything from renderer input here: mesh:enable
// takes no arguments from the renderer at all, and every value passed to
// the helper is a fixed production constant or a path main.js computed
// itself (getWindowsMeshBinPath()).
//
// Querying task state (Get-ScheduledTask) needs no elevation, so
// computeWindowsDaemonStatus() never elevates — only mesh:enable (the
// install path) does.

const WINDOWS_MESH_TASK_NAME = 'PotluckMesh';
const WINDOWS_MESH_INSTALL_TIMEOUT_MS = 60_000; // includes the UAC prompt wait

// Production defaults — same values baked into the macOS LaunchDaemon plist
// and passed to the Linux install helper above.
const WINDOWS_MESH_COORDINATOR_URL_DEFAULT = 'https://coordinator.trypotluck.ai';
const WINDOWS_MESH_DERP_URL_DEFAULT = 'tcp+tls://derp.trypotluck.ai:443';

/**
 * Absolute path to the fixed, app-shipped PowerShell install helper. In a
 * packaged build this is process.resourcesPath\mesh-install-helper.ps1 (see
 * the win.extraResources entry in package.json). In dev (unpackaged) there
 * is no packaged helper to elevate, so this returns null and mesh:enable
 * reports 'not_installed' with an explanatory detail — same "no dev-mode
 * daemon install" posture as getMeshHelperPath()/getLinuxMeshHelperPath().
 */
function getWindowsMeshHelperPath() {
  if (!app.isPackaged || process.platform !== 'win32') {
    return null;
  }
  return path.join(process.resourcesPath, 'mesh-install-helper.ps1');
}

/**
 * Absolute path to the SOURCE mesh binary this install should copy from
 * (see mesh-install-helper.ps1's header comment for why it copies rather
 * than execing this path directly — electron-builder's NSIS default is a
 * per-user install directory, not an admin-owned one). Mirrors
 * getLinuxMeshBinPath()'s resourcesPath resolution.
 */
function getWindowsMeshBinPath() {
  if (!app.isPackaged) return null;
  return path.join(process.resourcesPath, 'mesh', 'potluck-mesh.exe');
}

/**
 * Runs the install helper elevated via a single UAC prompt
 * (Start-Process -Verb RunAs — the Win32 ShellExecute 'runas' verb) and
 * waits for it to finish. Never rejects — same contract as
 * runMeshHelper()/runLinuxMeshHelper() above.
 *
 * `args` must only ever contain fixed production constants or values
 * main.js computed itself; see the big security-model comment above. Each
 * arg is embedded as a single-quoted PowerShell string literal (with `'`
 * doubled per PowerShell's own escaping rule) inside the -Command passed to
 * the UNELEVATED launcher powershell.exe process — execFile invokes that
 * launcher directly (no shell), so there is no separate shell-quoting layer
 * to worry about beyond PowerShell's own single-quote escaping.
 */
function runWindowsMeshHelperElevated(args) {
  return new Promise((resolve) => {
    const helperPath = getWindowsMeshHelperPath();
    if (!helperPath) {
      resolve({ ok: false, error: 'mesh-install-helper unavailable (dev mode or non-Windows)' });
      return;
    }
    const childArgs = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', helperPath, ...args];
    const psQuote = (s) => `'${String(s).replace(/'/g, "''")}'`;
    const psArgList = childArgs.map(psQuote).join(',');
    // 1223 = ERROR_CANCELLED — the standard Win32 code for "the user declined
    // the UAC prompt." Surfaced as a distinct, recognisable error string.
    const command =
      `$ErrorActionPreference='Stop'; try { `
      + `$p = Start-Process -FilePath 'powershell.exe' -ArgumentList @(${psArgList}) `
      + `-Verb RunAs -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode `
      + `} catch { exit 1223 }`;
    execFile(
      'powershell.exe',
      ['-NoProfile', '-Command', command],
      { timeout: WINDOWS_MESH_INSTALL_TIMEOUT_MS },
      (err) => {
        if (err) {
          const code = typeof err.code === 'number' ? err.code : null;
          const reason = code === 1223 ? 'elevation cancelled by user' : err.message;
          log.warn(`[mesh-daemon-windows] elevated helper failed: ${reason}`);
          resolve({ ok: false, error: reason });
          return;
        }
        resolve({ ok: true });
      },
    );
  });
}

/**
 * Queries the PotluckMesh Scheduled Task's state via a small PowerShell
 * one-liner. No elevation required to read task state (mirrors
 * systemctlIsActive() on Linux). Returns null if the task does not exist.
 */
function queryWindowsMeshTask() {
  return new Promise((resolve) => {
    const ps =
      `$t = Get-ScheduledTask -TaskName '${WINDOWS_MESH_TASK_NAME}' -ErrorAction SilentlyContinue; `
      + `if (-not $t) { Write-Output 'absent'; exit }; `
      + `$info = $t | Get-ScheduledTaskInfo; `
      + `Write-Output "$($t.State)|$($info.LastTaskResult)|$($info.NumberOfMissedRuns)"`;
    execFile('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', ps], { timeout: 5_000 }, (err, stdout) => {
      const out = String(stdout || '').trim();
      if (err || out === '' || out === 'absent') {
        resolve(null);
        return;
      }
      const [state, lastResultRaw, missedRunsRaw] = out.split('|');
      resolve({
        state: state || 'Unknown',
        lastResult: Number(lastResultRaw),
        missedRuns: Number(missedRunsRaw) || 0,
      });
    });
  });
}

async function computeWindowsDaemonStatus() {
  const task = await queryWindowsMeshTask();
  if (!task) {
    return { platform: 'win32', state: 'not_installed' };
  }

  if (task.state !== 'Running') {
    // LastTaskResult 0 = success (previous run exited cleanly — shouldn't
    // happen for a long-running daemon, but not itself a failure signal),
    // 267009 (0x41301) = "task is currently running" (a race with our own
    // read), 267011 (0x41303) = "task has not yet run". Anything else
    // non-zero is a real failure result from the last run attempt.
    const lr = task.lastResult;
    const isKnownBenign = lr === 0 || lr === 267009 || lr === 267011 || Number.isNaN(lr);
    if (!isKnownBenign || task.missedRuns >= 3) {
      return {
        platform: 'win32',
        state: 'failed',
        detail: `scheduled task state=${task.state}, last result=0x${(lr >>> 0).toString(16)}${
          task.missedRuns ? `, ${task.missedRuns} missed runs` : ''
        }`,
      };
    }
    return { platform: 'win32', state: 'starting', detail: `scheduled task state: ${task.state}` };
  }

  try {
    const res = await meshRequest({ method: 'GET', path: '/status' });
    if (res.status === 200) {
      return { platform: 'win32', state: 'running' };
    }
    return { platform: 'win32', state: 'starting', detail: `pipe responded with HTTP ${res.status}` };
  } catch (err) {
    const code = err && err.code ? err.code : (err && err.message) || 'pipe unreachable';
    return { platform: 'win32', state: 'starting', detail: code };
  }
}

ipcMain.handle('mesh:daemon-status', async () => {
  if (process.platform === 'darwin') {
    return computeMacDaemonStatus();
  }
  if (process.platform === 'win32') {
    return computeWindowsDaemonStatus();
  }
  // linux
  return computeLinuxDaemonStatus();
});

ipcMain.handle('mesh:enable', async () => {
  if (process.platform === 'linux') {
    const meshBin = getLinuxMeshBinPath();
    if (!meshBin) {
      return {
        platform: 'linux',
        state: 'not_installed',
        detail: 'mesh install unavailable in dev mode (no packaged mesh binary to point the unit at)',
      };
    }
    // Every argument here is either a fixed production constant or a value
    // main.js computed itself (this process's own uid/gid) — never
    // renderer-supplied. See the big comment above this section.
    const helperResult = await runLinuxMeshHelper([
      'install',
      '--coordinator-url', LINUX_MESH_COORDINATOR_URL_DEFAULT,
      '--derp-url', LINUX_MESH_DERP_URL_DEFAULT,
      '--mesh-bin', meshBin,
      '--uid', String(process.getuid ? process.getuid() : ''),
      '--gid', String(process.getgid ? process.getgid() : ''),
    ]);
    if (!helperResult.ok) {
      return { platform: 'linux', state: 'not_installed', detail: helperResult.error };
    }
    log.info('[mesh-daemon-linux] install helper reported success; confirming liveness.');
    return computeLinuxDaemonStatus();
  }

  if (process.platform === 'win32') {
    const meshBin = getWindowsMeshBinPath();
    if (!meshBin) {
      return {
        platform: 'win32',
        state: 'not_installed',
        detail: 'mesh install unavailable in dev mode (no packaged mesh binary to point the task at)',
      };
    }
    // Every argument here is a fixed production constant or a value main.js
    // computed itself (getWindowsMeshBinPath()) — never renderer-supplied.
    // See the big security-model comment above this section, and
    // mesh-install-helper.ps1's header comment for the elevated half.
    const helperResult = await runWindowsMeshHelperElevated([
      'install',
      '-CoordinatorUrl', WINDOWS_MESH_COORDINATOR_URL_DEFAULT,
      '-DerpUrl', WINDOWS_MESH_DERP_URL_DEFAULT,
      '-MeshBin', meshBin,
    ]);
    if (!helperResult.ok) {
      return { platform: 'win32', state: 'not_installed', detail: helperResult.error };
    }
    log.info('[mesh-daemon-windows] install helper reported success; confirming liveness.');
    return computeWindowsDaemonStatus();
  }

  if (process.platform !== 'darwin') {
    return {
      platform: process.platform,
      state: 'unsupported',
      detail: `mesh:enable not implemented on ${process.platform} (TODO)`,
    };
  }

  const helperResult = await runMeshHelper('register');
  if (!helperResult.ok) {
    return { platform: 'darwin', state: 'not_installed', detail: helperResult.error };
  }

  const token = helperResult.output;
  if (token === 'requiresApproval') {
    log.info('[mesh-daemon] registered — awaiting Login Items approval.');
    return { platform: 'darwin', state: 'requires_approval' };
  }
  if (token === 'enabled') {
    log.info('[mesh-daemon] registered and enabled.');
    // Confirm liveness the same way daemon-status does, rather than
    // optimistically reporting 'running' before the socket answers.
    return computeMacDaemonStatus();
  }
  return { platform: 'darwin', state: 'not_installed', detail: `unexpected status after register: ${token}` };
});

ipcMain.handle('mesh:open-login-items', async () => {
  if (process.platform !== 'darwin') {
    return { platform: process.platform, ok: false };
  }
  const helperResult = await runMeshHelper('open-login-items');
  return { platform: 'darwin', ok: helperResult.ok };
});

// ─── Cloud API bridge ────────────────────────────────────────────────────────
//
// The packaged renderer loads from file:// (Origin: null), so browser CORS
// blocks it from reading responses off api.trypotluck.ai even with a bearer
// token. We proxy cloud calls through the main process (Node fetch, no CORS)
// and keep the opaque session token HERE in the main process, never in renderer
// JS — preserving the httpOnly-cookie security property the web client has. The
// cloud API accepts `Authorization: Bearer <token>` (services/api
// middleware/auth.py). In dev the renderer is http://localhost:5173, which is
// CORS-allowlisted, so it uses plain fetch + cookie and never hits this bridge.

const CLOUD_API_BASE =
  process.env.POTLUCK_CLOUD_API_URL || 'https://api.trypotluck.ai';
const CLOUD_REQUEST_TIMEOUT_MS = 15_000;

// undefined = not yet loaded from disk; null = no session; string = the token.
let cloudSessionToken;

function cloudTokenPath() {
  return path.join(app.getPath('userData'), 'cloud-session');
}

function loadCloudToken() {
  if (process.platform !== 'win32' && (app.isPackaged || attachedRuntime)) {
    try {
      const shared = readMemberSession(CLOUD_API_BASE);
      if (shared !== undefined) return shared.token;
    } catch (_) { return null; } // A private/foreign-origin record must never fall back.
  }
  if (cloudSessionToken !== undefined) return cloudSessionToken;
  try {
    cloudSessionToken = fs.readFileSync(cloudTokenPath(), 'utf8').trim() || null;
  } catch (_) {
    cloudSessionToken = null;
  }
  return cloudSessionToken;
}

async function migrateMemberSession() {
  if (process.platform === 'win32' || !app.isPackaged) return;
  try {
    const legacy = fs.existsSync(cloudTokenPath()) ? fs.readFileSync(cloudTokenPath(), 'utf8').trim() : null;
    if (!legacy) return;
    const response = await sidecarRequest('POST', '/account/session/import', { token: legacy });
    if (response.ok) {
      fs.rmSync(cloudTokenPath(), { force: true });
      cloudSessionToken = undefined;
    } else log.warn(`[account] Shared session migration returned HTTP ${response.status}; legacy state retained.`);
  } catch (_) { log.warn('[account] Shared session migration unavailable; legacy state retained.'); }
}

// ─── Contributor session hand-off ────────────────────────────────────────────
//
// The in-process contributor inside the sidecar identifies this machine to the
// coordinator with the member's session bearer. The sidecar never reads the
// token file itself; main hands the token over loopback (sidecar bearer on the
// request) whenever it changes and whenever the sidecar comes up. The renderer
// never sees it.

const CONTRIBUTOR_SESSION_SYNC_TIMEOUT_MS = 2_500;

function sidecarRequest(method, reqPath, body) {
  const headers = { Authorization: `Bearer ${SIDECAR_TOKEN}` };
  const init = { method, headers };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), reqPath.startsWith('/account')
    ? CLOUD_REQUEST_TIMEOUT_MS + 2_000 : CONTRIBUTOR_SESSION_SYNC_TIMEOUT_MS);
  init.signal = controller.signal;
  return fetch(`http://127.0.0.1:${PYTHON_PORT}${reqPath}`, init).finally(() => clearTimeout(timer));
}

// Resolves true when a session was handed to the sidecar, false when there was
// none to hand over or the sidecar did not take it.
async function syncContributorSession({ allowClear = true } = {}) {
  const token = loadCloudToken();
  // Opening a signed-out app must not sign out a runtime started from the CLI.
  if (!token && attachedRuntime && !allowClear) return false;
  try {
    const res = token
      ? await sidecarRequest('PUT', '/contributor/session', { token })
      : await sidecarRequest('DELETE', '/contributor/session');
    if (!res.ok) {
      log.warn(`[contributor] session sync returned HTTP ${res.status}`);
      return false;
    }
    return Boolean(token);
  } catch (err) {
    log.warn(`[contributor] session sync failed: ${err.message}`);
    return false;
  }
}

/**
 * After a (re)spawn the sidecar takes a few seconds to answer; wait for
 * /health and then hand over the session. Fire-and-forget from the spawn
 * path so a crash-restart re-arms the contributor without the renderer's
 * involvement.
 */
async function syncContributorSessionWhenReady() {
  const ready = await waitForSidecar();
  if (ready) await syncContributorSession({ allowClear: false });
}

// The renderer's circle chat goes through the sidecar, which answers 401 when it
// holds no session (it restarted without us, or never got one). The renderer
// asks for the hand-off again here; no token goes in or comes out, only whether
// the sidecar now has one.
ipcMain.handle('cloud:resync-engine-session', () => syncContributorSession({ allowClear: false }));

ipcMain.handle('cloud:set-token', async (_evt, token) => {
  if (process.platform !== 'win32' && (app.isPackaged || attachedRuntime)) {
    const response = token
      ? await sidecarRequest('PUT', '/account/session', { token })
      : await sidecarRequest('POST', '/account/logout', { local_only: true });
    if (!response.ok) throw new Error('Could not save the shared member session. Check the local engine and retry.');
    fs.rmSync(cloudTokenPath(), { force: true });
    cloudSessionToken = undefined;
    return;
  }
  cloudSessionToken = typeof token === 'string' && token.length > 0 ? token : null;
  try {
    if (cloudSessionToken) {
      // 0600: readable only by the current user. TODO(v0.5+): wrap with
      // electron safeStorage where the OS keychain is available.
      fs.writeFileSync(cloudTokenPath(), cloudSessionToken, { mode: 0o600 });
    } else {
      fs.rmSync(cloudTokenPath(), { force: true });
    }
  } catch (err) {
    log.warn(`[cloud] failed to persist session token: ${err.message}`);
  }
  void syncContributorSession();
});

ipcMain.handle('cloud:has-token', () => {
  return loadCloudToken() != null;
});

ipcMain.handle('cloud:clear-token', async () => {
  if (process.platform !== 'win32' && (app.isPackaged || attachedRuntime)) {
    const response = await sidecarRequest('POST', '/account/logout', { local_only: true });
    if (!response.ok) throw new Error('Could not clear the shared member session. Start the local engine and retry.');
  }
  cloudSessionToken = null;
  try {
    fs.rmSync(cloudTokenPath(), { force: true });
  } catch (_) {
    /* already gone */
  }
  void syncContributorSession();
});

ipcMain.handle('cloud:request', async (_evt, method, reqPath, body) => {
  const allowed = new Set(['GET', 'POST', 'DELETE']);
  if (typeof method !== 'string' || !allowed.has(method)) {
    return { status: 400, body: { detail: 'method not allowed' } };
  }
  // Path only; the base host is fixed here so the renderer can never turn this
  // into an open proxy to an arbitrary origin.
  if (typeof reqPath !== 'string' || !reqPath.startsWith('/')) {
    return { status: 400, body: { detail: 'path must start with /' } };
  }

  const headers = { Accept: 'application/json', 'X-Potluck-Client-Version': app.getVersion() };
  const token = loadCloudToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const init = { method, headers };
  if (body !== undefined && body !== null) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CLOUD_REQUEST_TIMEOUT_MS);
  init.signal = controller.signal;
  try {
    const res = await fetch(`${CLOUD_API_BASE}${reqPath}`, init);
    const text = await res.text();
    let parsed = null;
    if (text.length > 0) {
      try {
        parsed = JSON.parse(text);
      } catch (_) {
        parsed = { raw: text };
      }
    }
    return { status: res.status, body: parsed };
  } catch (err) {
    return {
      status: 0,
      body: { detail: err && err.message ? err.message : 'network error' },
    };
  } finally {
    clearTimeout(timer);
  }
});

// ─── Coordinator session-auth bridge ────────────────────────────────────────
//
// A small, path-allowlisted proxy for the handful of coordinator routes that
// require the member's login session rather than the (optional) coordinator
// token: POST /mesh/households + POST /mesh/pairing-tokens (household
// bootstrap — coordinator/routers/mesh.py), POST /me/device-keys (device key
// pre-registration for REQUIRE_REGISTERED_KEYS=true), the trusted-circle
// + identity routes (GET /me/identity, GET/POST /me/circles, DELETE
// /me/circles/{id}, POST /me/circles/{id}/members, DELETE
// /me/circles/{id}/members/{pubkey}, plus the invite and membership routes:
// POST /circles/invites/preview, POST /circles/invites/accept,
// GET/POST /me/circles/{id}/invites, DELETE /me/circles/{id}/invites/{id},
// DELETE /me/circles/{id}/membership, DELETE /me/circles/{id}/people/{id};
// coordinator's routers/circles.py), and
// the credit/contribution routes (GET /me/credit-balance, GET
// /me/credit-history, GET /me/contributions, GET /me/contributions/weekly —
// coordinator's routers/me.py), and GET /jobs/stats (own requester-side job
// counts — coordinator/routers/jobs.py). All of these resolve identity via
// `resolve_session_member_id`, which accepts
// an `Authorization: Bearer <session token>` header as the desktop-app
// fallback for a cross-origin cookie (see
// coordinator/middleware/session_auth.py).
//
// Unlike `cloud:request` above, the target ORIGIN is caller-supplied: the
// coordinator is a member-operated node the user points the app at (LAN IP,
// Tailscale IP, or the hosted default — see connectionStore.ts), not a fixed
// api.trypotluck.ai host. That's an intentional part of the mesh design, not
// a new open-proxy hole: the renderer can already reach any coordinator URL
// directly via plain fetch() (CORS on the coordinator allows file:///
// localhost origins for exactly this). What this bridge protects is the
// bearer token itself — it stays main-process-only, same as cloud:request,
// and is attached here rather than ever being handed to renderer JS. The
// route allowlist below is defence in depth so this can't become a generic
// "attach my session token to any request" primitive: static routes are
// exact-matched (method + path), and the two circle routes with a dynamic
// segment are matched by a tight pattern rather than a wildcard — no `*`,
// no free-form path-prefix check.
const COORDINATOR_SESSION_METHODS = new Set(['GET', 'POST', 'DELETE']);

// Static routes — exact `METHOD path` match.
const COORDINATOR_SESSION_EXACT_ROUTES = new Set([
  'POST /mesh/households',
  'GET /mesh/households/current',
  'POST /mesh/pairing-tokens',
  'POST /me/device-keys',
  'GET /me/identity',
  'GET /me/circles',
  'POST /me/circles',
  // Invites are looked up and accepted by code, which travels in the body.
  'POST /circles/invites/preview',
  'POST /circles/invites/accept',
  'GET /me/credit-balance',
  'GET /me/credit-history',
  'GET /me/contributions',
  'GET /me/contributions/weekly',
  'GET /jobs/stats',
]);

// Dynamic circle routes. `id` is the circle's Postgres UUID; `pubkey` is a
// contributor device key, base64url-encoded with no padding (see
// potluck_local/contributor/keys.py:public_key_b64), bounded to the
// coordinator's _PUBKEY_MAX_LEN (512 — circles.py). Both are validated by
// regex, not just split on `/`, so a crafted path segment (e.g. containing
// `../` or another `/`) can't smuggle a different route through the proxy.
// Invite and person ids are UUIDs: the patterns below accept only that shape
// in the second segment, and the check in isAllowedCoordinatorSessionRoute
// still runs on it.
const COORDINATOR_CIRCLE_ID_RE = /^[0-9a-f-]{36}$/i;
const COORDINATOR_PUBKEY_RE = /^[A-Za-z0-9_-]{1,512}$/;
const COORDINATOR_SESSION_DYNAMIC_ROUTES = [
  { method: 'DELETE', pattern: /^\/me\/circles\/([^/]+)$/ },
  { method: 'GET', pattern: /^\/me\/circles\/([^/]+)\/availability$/ },
  { method: 'POST', pattern: /^\/me\/circles\/([^/]+)\/members$/ },
  { method: 'DELETE', pattern: /^\/me\/circles\/([^/]+)\/members\/([^/]+)$/ },
  // Owner makes and lists invites, and withdraws one.
  { method: 'POST', pattern: /^\/me\/circles\/([^/]+)\/invites$/ },
  { method: 'GET', pattern: /^\/me\/circles\/([^/]+)\/invites$/ },
  { method: 'DELETE', pattern: /^\/me\/circles\/([^/]+)\/invites\/([0-9a-fA-F-]{36})$/ },
  // A member leaves; the owner removes a person.
  { method: 'DELETE', pattern: /^\/me\/circles\/([^/]+)\/membership$/ },
  { method: 'DELETE', pattern: /^\/me\/circles\/([^/]+)\/people\/([0-9a-fA-F-]{36})$/ },
  // Member removes a (stale/ghost) machine from their own household.
  { method: 'DELETE', pattern: /^\/mesh\/households\/current\/machines\/([0-9a-fA-F-]{36})$/ },
];

function isAllowedCoordinatorSessionRoute(method, reqPath) {
  // The credit/contribution GETs carry query params (?days=, ?weeks=, etc.);
  // match the allowlist against the path only, never the query string.
  const queryIdx = reqPath.indexOf('?');
  const pathOnly = queryIdx === -1 ? reqPath : reqPath.slice(0, queryIdx);
  if (COORDINATOR_SESSION_EXACT_ROUTES.has(`${method} ${pathOnly}`)) return true;
  for (const route of COORDINATOR_SESSION_DYNAMIC_ROUTES) {
    if (route.method !== method) continue;
    const match = route.pattern.exec(pathOnly);
    if (!match) continue;
    const [, circleId, pubkey] = match;
    if (!COORDINATOR_CIRCLE_ID_RE.test(circleId)) continue;
    if (pubkey !== undefined && !COORDINATOR_PUBKEY_RE.test(pubkey)) continue;
    return true;
  }
  return false;
}

ipcMain.handle(
  'coordinator:session-request',
  async (_evt, baseUrl, method, reqPath, body) => {
    if (typeof method !== 'string' || !COORDINATOR_SESSION_METHODS.has(method)) {
      return { status: 400, body: { detail: 'method not allowed' } };
    }
    if (typeof reqPath !== 'string' || !isAllowedCoordinatorSessionRoute(method, reqPath)) {
      return { status: 400, body: { detail: 'path not allowed' } };
    }

    let target;
    try {
      target = new URL(reqPath, baseUrl);
    } catch (_) {
      return { status: 400, body: { detail: 'invalid coordinator url' } };
    }
    if (target.protocol !== 'http:' && target.protocol !== 'https:') {
      return { status: 400, body: { detail: 'unsupported protocol' } };
    }

    const headers = { Accept: 'application/json', 'X-Potluck-Client-Version': app.getVersion() };
    const token = loadCloudToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const init = { method, headers };
    if (body !== undefined && body !== null) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), CLOUD_REQUEST_TIMEOUT_MS);
    init.signal = controller.signal;
    try {
      const res = await fetch(target, init);
      const text = await res.text();
      let parsed = null;
      if (text.length > 0) {
        try {
          parsed = JSON.parse(text);
        } catch (_) {
          parsed = { raw: text };
        }
      }
      return { status: res.status, body: parsed };
    } catch (err) {
      return {
        status: 0,
        body: { detail: err && err.message ? err.message : 'network error' },
      };
    } finally {
      clearTimeout(timer);
    }
  },
);

// Lets the renderer tell main which coordinator origin is currently active
// (connectionStore.ts's coordinatorUrl — user-overridable and runtime-mutable
// via adoptSidecarUrl/hydrateFromSidecar, so main cannot hardcode it). Stores
// the ORIGIN only, not the full URL, and only via new URL().origin — never a
// raw string compare — so the onBeforeSendHeaders injector above can scope the
// member session bearer to exactly this host. Renderer calls this once at
// startup and again on every coordinatorUrl change (see connectionStore.ts's
// initCoordinatorBaseSync). Malformed/empty input is ignored, leaving the
// previous value (or null) in place rather than clearing it on a transient
// bad call.
ipcMain.handle('coordinator:set-base', (_evt, baseUrl) => {
  if (typeof baseUrl !== 'string' || baseUrl.length === 0) return;
  try {
    coordinatorOrigin = new URL(baseUrl).origin;
  } catch (_) {
    // Ignore — keep whatever coordinatorOrigin was already set to.
  }
});

// ─── Auto-updater ────────────────────────────────────────────────────────────

const UPDATE_CHECK_INITIAL_DELAY_MS = 10_000; // 10s after startup
const UPDATE_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000; // 6 hours

function setupAutoUpdater() {
  // Only run the updater when this build can actually self-update: packaged AND
  // app-update.yml present (see isUpdaterConfigured). A `--dir` / dogfood /
  // unsigned local build is packaged but has no app-update.yml, so a check would
  // throw "ENOENT ... app-update.yml" and surface a bogus "update failed" banner.
  if (!isUpdaterConfigured()) {
    log.info('[updater] Disabled for this build (no app-update.yml — dev/dir/unsigned).');
    return;
  }

  autoUpdater.logger = log;
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('update-available', (info) => {
    log.info(`[updater] Update available: ${info.version}`);
    if (mainWindow) {
      mainWindow.webContents.send('update-available', info.version);
    }
  });

  // Forwarded so the UI can show "up to date" rather than sitting on a
  // perpetual "checking..." state after a check comes back negative.
  autoUpdater.on('update-not-available', (info) => {
    log.info(`[updater] No update available (current: ${info && info.version}).`);
    if (mainWindow) {
      mainWindow.webContents.send('update-not-available');
    }
  });

  autoUpdater.on('download-progress', (p) => {
    if (mainWindow) {
      mainWindow.webContents.send('update-progress', Math.round(p.percent));
    }
  });

  autoUpdater.on('update-downloaded', () => {
    log.info('[updater] Update downloaded — ready to install on restart.');
    updateDownloadStarted = false;
    if (mainWindow) {
      mainWindow.webContents.send('update-downloaded');
    }
  });

  // Only surface an error banner when the user actually started a download or
  // install (updateDownloadStarted). A failed background CHECK — feed
  // unreachable at launch, transient network, a releases.* hiccup — is logged
  // but stays silent, so a released user is never nagged for something they did
  // not do.
  autoUpdater.on('error', (err) => {
    log.error(`[updater] Error: ${err.message}`);
    if (updateDownloadStarted && mainWindow) {
      updateDownloadStarted = false;
      mainWindow.webContents.send('update-error', err.message);
    }
  });

  // Initial check shortly after startup, then re-check on a fixed interval —
  // autoDownload stays false throughout, so these only ever surface an
  // 'update-available'/'update-not-available' event; the user consents to
  // the actual download from the UI (update:download / updater:download).
  setTimeout(() => {
    autoUpdater.checkForUpdates().catch((err) => {
      log.error(`[updater] Initial check failed: ${err.message}`);
    });
  }, UPDATE_CHECK_INITIAL_DELAY_MS);

  setInterval(() => {
    autoUpdater.checkForUpdates().catch((err) => {
      log.error(`[updater] Periodic check failed: ${err.message}`);
    });
  }, UPDATE_CHECK_INTERVAL_MS);
}

// ─── Graceful shutdown ───────────────────────────────────────────────────────

/**
 * Sends SIGTERM to the sidecar and, if it has not exited within SIGKILL_TIMEOUT_MS,
 * forcibly kills it with SIGKILL (or taskkill on Windows).
 * Returns a Promise that resolves once the process is gone or the timeout elapses.
 */
function killSidecar({ shutdownRequested = false } = {}) {
  return new Promise((resolve) => {
    if (!pythonProcess) {
      resolve();
      return;
    }

    const proc = pythonProcess;
    pythonProcess = null;

    let settled = false;
    let gracefulTimer;
    let killTimer;
    const done = () => {
      if (!settled) {
        settled = true;
        clearTimeout(gracefulTimer);
        clearTimeout(killTimer);
        resolve();
      }
    };

    proc.once('exit', done);

    const terminate = () => {
      if (settled) return;
      try { proc.kill('SIGTERM'); }
      catch (err) { log.warn(`[sidecar] SIGTERM failed: ${err.message}`); }
      killTimer = setTimeout(() => {
        if (settled) return;
        log.warn('[sidecar] SIGTERM timeout — sending SIGKILL.');
        try {
          proc.kill('SIGKILL');
        } catch (err) {
          log.warn(`[sidecar] SIGKILL failed: ${err.message}`);
        }
        // Give Windows an extra beat then resolve regardless.
        setTimeout(done, 500);
      }, SIGKILL_TIMEOUT_MS);
    };

    // Let /shutdown finish and remove the shared runtime record before sending
    // a signal. Uvicorn re-raises SIGTERM after serving, skipping that cleanup.
    if (shutdownRequested) gracefulTimer = setTimeout(terminate, SIGKILL_TIMEOUT_MS);
    else terminate();
  });
}

// ─── App lifecycle ───────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  // Secondary instance (lost the single-instance lock) is on its way out — do
  // not boot a sidecar or window. The primary already got our argv via
  // 'second-instance'.
  if (!gotSingleInstanceLock) return;

  log.info(`[app] Starting Potluck AI ${app.getVersion()} on ${process.platform}/${process.arch}`);

  // Cold start via deep link on Windows/Linux: the URL is in our own argv.
  // Buffer it before the window is created so the renderer pulls it on mount.
  // Goes through the same isAuthCallbackDeepLink gate as every other entry
  // point — see handleDeepLink.
  if (process.platform !== 'darwin') {
    const coldStartLink = extractDeepLink(process.argv);
    if (coldStartLink && isAuthCallbackDeepLink(coldStartLink)) {
      pendingDeepLink = coldStartLink;
    }
  }

  // Override the Dock icon at runtime. Required on macOS dev, where the host
  // Electron.app's icon would otherwise show. Harmless on other platforms
  // (app.dock is undefined there).
  if (process.platform === 'darwin' && app.dock) {
    const iconPath = path.join(__dirname, 'icons', 'icon.png');
    const dockIcon = nativeImage.createFromPath(iconPath);
    if (!dockIcon.isEmpty()) {
      app.dock.setIcon(dockIcon);
    } else {
      log.warn(`[app] Dock icon missing or empty at ${iconPath}`);
    }
  }

  await startPythonSidecar();

  const sidecarReady = await waitForSidecar();
  if (!sidecarReady) {
    const { response } = await dialog.showMessageBox({
      type: 'error',
      title: 'Potluck AI — startup error',
      message: 'The local AI engine failed to start.',
      detail:
        'The backend did not respond within the startup timeout.\n\n' +
        'Log file: ' +
        path.join(app.getPath('userData'), 'logs', 'main.log'),
      buttons: ['Retry', 'Quit'],
      defaultId: 0,
      cancelId: 1,
    });

    if (response === 0) {
      // Retry: restart the whole launch sequence.
      app.relaunch();
      app.exit(0);
      return;
    }
    app.exit(1);
    return;
  }
  // Dev mode runs the sidecar outside this process (no spawn hook fires), so
  // hand the member session over here as well. Idempotent in production.
  await migrateMemberSession();
  void syncContributorSession({ allowClear: false });

  await createWindow();
  setupAutoUpdater();
});

app.on('activate', async () => {
  // macOS: re-create the window when the dock icon is clicked and no windows are open.
  // Guard against a dead or restarting sidecar: if pythonProcess is null the sidecar
  // is mid-restart; opening a window against 127.0.0.1:8321 would get ECONNREFUSED
  // for every API call until the restart completes. Skip window creation in that case —
  // the restart handler will not re-open a window, but the next crash-free activate
  // (or the app.whenReady path after a future relaunch) will.
  if (BrowserWindow.getAllWindows().length === 0 && (pythonProcess || attachedRuntime || !app.isPackaged)) {
    await createWindow();
  }
});

app.on('window-all-closed', () => {
  // On macOS it is conventional to keep the app (and sidecar) alive until the
  // user explicitly quits via Cmd+Q or the menu bar.
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', async (event) => {
  if (!app.isQuitting) {
    event.preventDefault();
    app.isQuitting = true;
    clearInterval(sharedRuntimePoll);
    if (attachedRuntime) {
      log.info('[app] Leaving the shared runtime running.');
      // Electron ignores a synchronous re-entrant quit while the first
      // before-quit event is still being cancelled. Finish that event first.
      setImmediate(() => app.quit());
      return;
    }
    log.info('[app] Shutting down sidecar before quit…');

    let shutdownRequested = false;
    try {
      // Best-effort HTTP shutdown signal; the sidecar can do cleanup on its end.
      const response = await Promise.race([
        fetch(`http://127.0.0.1:${PYTHON_PORT}/shutdown`, {
          method: 'POST', headers: { Authorization: `Bearer ${SIDECAR_TOKEN}` },
        }),
        new Promise((r) => setTimeout(r, 1000)),
      ]);
      shutdownRequested = response?.ok === true;
    } catch (_) {
      // Ignore — the sidecar may already be down.
    }

    await killSidecar({ shutdownRequested });
    log.info('[app] Sidecar terminated. Quitting.');
    app.quit();
  }
});
