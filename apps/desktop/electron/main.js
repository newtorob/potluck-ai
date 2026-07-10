'use strict';

const { app, BrowserWindow, shell, ipcMain, dialog, Notification, nativeImage, session } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const path = require('path');
const http = require('http');
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

/**
 * Deliver a deep link to the renderer, or buffer it until the renderer is ready.
 * Never logs the query string — it carries the one-time magic-link token.
 */
function handleDeepLink(url) {
  if (!url) return;
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

  // Inject the sidecar auth token into every renderer request to the local API.
  // Production only: the same token is passed to the spawned sidecar via env, and
  // the sidecar rejects non-exempt requests that lack it. In dev the sidecar is
  // started by dev:python with no token (gate off), so injecting one here would
  // mismatch and 401 every call — hence the app.isPackaged guard.
  if (app.isPackaged) {
    session.defaultSession.webRequest.onBeforeSendHeaders(
      {
        urls: [
          `http://127.0.0.1:${PYTHON_PORT}/*`,
          `http://localhost:${PYTHON_PORT}/*`,
        ],
      },
      (details, callback) => {
        details.requestHeaders['Authorization'] = `Bearer ${SIDECAR_TOKEN}`;
        callback({ requestHeaders: details.requestHeaders });
      }
    );

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

  // Route all window.open() / anchor target="_blank" clicks to the system browser.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Also intercept will-navigate to external URLs.
  mainWindow.webContents.on('will-navigate', (event, url) => {
    const parsed = new URL(url);
    const isLocal =
      parsed.hostname === 'localhost' ||
      parsed.hostname === '127.0.0.1' ||
      parsed.protocol === 'file:';
    if (!isLocal) {
      event.preventDefault();
      shell.openExternal(url);
    }
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

// ─── Auto-updater IPC (renderer-driven download + install) ───────────────────
// autoDownload is off, so the renderer decides when to pull the update (banner
// "Download") and when to restart into it ("Restart"). These are registered
// unconditionally so the facade never rejects; in dev there is simply no update.
ipcMain.handle('updater:download', async () => {
  log.info('[updater] Renderer requested download.');
  return autoUpdater.downloadUpdate();
});

ipcMain.handle('updater:quit-and-install', () => {
  log.info('[updater] Renderer requested restart-to-install.');
  // Defer so the IPC reply flushes before the app tears itself down.
  setImmediate(() => autoUpdater.quitAndInstall());
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

// ─── Auto-updater ────────────────────────────────────────────────────────────

function setupAutoUpdater() {
  // Only run the updater inside a packaged build; electron-updater will throw
  // in development because there is no published release to compare against.
  if (!app.isPackaged) return;

  autoUpdater.logger = log;
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('update-available', (info) => {
    log.info(`[updater] Update available: ${info.version}`);
    if (mainWindow) {
      mainWindow.webContents.send('update-available', info.version);
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

  autoUpdater.checkForUpdatesAndNotify();
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
  if (process.platform !== 'darwin') {
    pendingDeepLink = extractDeepLink(process.argv) || pendingDeepLink;
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
