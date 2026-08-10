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
const SIDECAR_TOKEN = crypto.randomBytes(32).toString('hex');

// ─── State ───────────────────────────────────────────────────────────────────

let pythonProcess = null;
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

function startPythonSidecar() {
  const sidecarPath = getSidecarPath();

  if (!sidecarPath) {
    log.info('[sidecar] Dev mode — expecting Python sidecar started via dev:python script.');
    return;
  }

  log.info(`[sidecar] Spawning: ${sidecarPath} --port ${PYTHON_PORT}`);

  pythonProcess = spawn(sidecarPath, ['--port', String(PYTHON_PORT)], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {
      ...process.env,
      POTLUCK_PORT: String(PYTHON_PORT),
      POTLUCK_SIDECAR_TOKEN: SIDECAR_TOKEN,
    },
  });

  pythonProcess.on('spawn', () => {
    // Reset the crash-loop counter once the process successfully starts.
    // This way only *consecutive* rapid failures count against the limit,
    // not the total lifetime crash count across a long session.
    restartAttempts = 0;
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

    if (app.isQuitting) {
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
      const res = await fetch(`http://127.0.0.1:${PYTHON_PORT}/health`);
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
  // Linux: XDG_RUNTIME_DIR or /run/user/$UID fallback.
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

ipcMain.handle('mesh:daemon-status', async () => {
  if (process.platform === 'darwin') {
    return computeMacDaemonStatus();
  }
  if (process.platform === 'win32') {
    // TODO(windows): supervise potluck-mesh as a Windows Service (installed
    // at .msi install time, or registered on demand) — no SMAppService
    // equivalent exists on Windows, so this needs its own mechanism, not a
    // port of the macOS one. Not implemented.
    return {
      platform: 'win32',
      state: 'unsupported',
      detail: 'Windows mesh daemon supervision not implemented (TODO)',
    };
  }
  // linux
  // TODO(linux): supervise potluck-mesh as a systemd system unit, most
  // likely installed/enabled via a one-time `pkexec` prompt from this IPC
  // handler (systemd has no user-consent-free root-daemon API like
  // SMAppService). Not implemented.
  return {
    platform: 'linux',
    state: 'unsupported',
    detail: 'Linux mesh daemon supervision not implemented (TODO)',
  };
});

ipcMain.handle('mesh:enable', async () => {
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
  if (cloudSessionToken !== undefined) return cloudSessionToken;
  try {
    cloudSessionToken = fs.readFileSync(cloudTokenPath(), 'utf8').trim() || null;
  } catch (_) {
    cloudSessionToken = null;
  }
  return cloudSessionToken;
}

ipcMain.handle('cloud:set-token', (_evt, token) => {
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
});

ipcMain.handle('cloud:has-token', () => {
  return loadCloudToken() != null;
});

ipcMain.handle('cloud:clear-token', () => {
  cloudSessionToken = null;
  try {
    fs.rmSync(cloudTokenPath(), { force: true });
  } catch (_) {
    /* already gone */
  }
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
// /me/circles/{id}/members/{pubkey} — coordinator's routers/circles.py), and
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
  'POST /mesh/pairing-tokens',
  'POST /me/device-keys',
  'GET /me/identity',
  'GET /me/circles',
  'POST /me/circles',
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
const COORDINATOR_CIRCLE_ID_RE = /^[0-9a-f-]{36}$/i;
const COORDINATOR_PUBKEY_RE = /^[A-Za-z0-9_-]{1,512}$/;
const COORDINATOR_SESSION_DYNAMIC_ROUTES = [
  { method: 'DELETE', pattern: /^\/me\/circles\/([^/]+)$/ },
  { method: 'POST', pattern: /^\/me\/circles\/([^/]+)\/members$/ },
  { method: 'DELETE', pattern: /^\/me\/circles\/([^/]+)\/members\/([^/]+)$/ },
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
    if (mainWindow) {
      mainWindow.webContents.send('update-downloaded');
    }
  });

  autoUpdater.on('error', (err) => {
    log.error(`[updater] Error: ${err.message}`);
    if (mainWindow) {
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
function killSidecar() {
  return new Promise((resolve) => {
    if (!pythonProcess) {
      resolve();
      return;
    }

    const proc = pythonProcess;
    pythonProcess = null;

    let settled = false;
    const done = () => {
      if (!settled) {
        settled = true;
        resolve();
      }
    };

    proc.once('exit', done);

    // Attempt graceful SIGTERM first.
    try {
      proc.kill('SIGTERM');
    } catch (err) {
      log.warn(`[sidecar] SIGTERM failed: ${err.message}`);
    }

    // Escalate to SIGKILL after timeout.
    const killTimer = setTimeout(() => {
      if (!settled) {
        log.warn('[sidecar] SIGTERM timeout — sending SIGKILL.');
        try {
          proc.kill('SIGKILL');
        } catch (err) {
          log.warn(`[sidecar] SIGKILL failed: ${err.message}`);
        }
        // Give Windows an extra beat then resolve regardless.
        setTimeout(done, 500);
      }
    }, SIGKILL_TIMEOUT_MS);

    proc.once('exit', () => clearTimeout(killTimer));
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

  startPythonSidecar();

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
  if (BrowserWindow.getAllWindows().length === 0 && (pythonProcess || !app.isPackaged)) {
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
    log.info('[app] Shutting down sidecar before quit…');

    try {
      // Best-effort HTTP shutdown signal; the sidecar can do cleanup on its end.
      await Promise.race([
        fetch(`http://127.0.0.1:${PYTHON_PORT}/shutdown`, { method: 'POST' }),
        new Promise((r) => setTimeout(r, 1000)),
      ]);
    } catch (_) {
      // Ignore — the sidecar may already be down.
    }

    await killSidecar();
    log.info('[app] Sidecar terminated. Quitting.');
    app.quit();
  }
});
