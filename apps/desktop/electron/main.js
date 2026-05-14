'use strict';

const { app, BrowserWindow, shell, ipcMain, dialog, Notification, nativeImage } = require('electron');
const { spawn } = require('child_process');
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

// ─── State ───────────────────────────────────────────────────────────────────

let pythonProcess = null;
let mainWindow = null;
let restartAttempts = 0;

// app.isQuitting is set in before-quit to stop the crash-restart loop.
app.isQuitting = false;

// ─── Logging setup ───────────────────────────────────────────────────────────

log.transports.file.resolvePathFn = () =>
  path.join(app.getPath('userData'), 'logs', 'main.log');
log.transports.file.level = 'debug';
log.transports.console.level = 'debug';

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
  return path.join(process.resourcesPath, 'python', binaryName);
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
    env: { ...process.env, POTLUCK_PORT: String(PYTHON_PORT) },
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
      log.error('[sidecar] Max restart attempts reached. Showing error dialog.');
      dialog
        .showMessageBox({
          type: 'error',
          title: 'Potluck AI — backend error',
          message: 'The local AI engine has crashed and could not restart.',
          detail:
            'Check the log file for details.\n' +
            path.join(app.getPath('userData'), 'logs', 'main.log'),
          buttons: ['Quit'],
        })
        .then(() => app.quit());
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

  if (process.env.NODE_ENV === 'development') {
    mainWindow.loadURL('http://localhost:5173');
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  } else {
    // In production the React bundle sits at apps/desktop/frontend/dist/index.html
    // relative to this file (which lives in the app's Resources/ root after packaging).
    mainWindow.loadFile(
      path.join(__dirname, '..', 'frontend', 'dist', 'index.html')
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
    new Notification({ title: String(title), body: String(body) }).show();
  }
});

ipcMain.handle('get-app-version', () => app.getVersion());

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

  autoUpdater.on('update-downloaded', () => {
    log.info('[updater] Update downloaded — will install on next quit.');
    if (mainWindow) {
      mainWindow.webContents.send('update-downloaded');
    }
  });

  autoUpdater.on('error', (err) => {
    log.error(`[updater] Error: ${err.message}`);
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
  log.info(`[app] Starting Potluck AI ${app.getVersion()} on ${process.platform}/${process.arch}`);

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

  // Reset the restart counter once we know the sidecar is healthy.
  restartAttempts = 0;

  await createWindow();
  setupAutoUpdater();
});

app.on('activate', async () => {
  // macOS: re-create the window when the dock icon is clicked and no windows are open.
  if (BrowserWindow.getAllWindows().length === 0) {
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
