'use strict';

/**
 * Preload script — runs in the renderer process before any page JS loads.
 *
 * Rules:
 *   - contextIsolation: true  (enforced in main.js webPreferences)
 *   - nodeIntegration: false  (enforced in main.js webPreferences)
 *   - ONLY expose the minimum API the UI actually needs.
 *   - NEVER expose ipcRenderer directly.
 *   - All IPC calls are wrapped in named, typed functions.
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('potluck', {
  // ── Read-only process info ─────────────────────────────────────────────────
  platform: process.platform,
  arch: process.arch,
  versions: {
    electron: process.versions.electron,
    node: process.versions.node,
    chrome: process.versions.chrome,
  },

  // ── IPC: Electron API proxies ──────────────────────────────────────────────

  /**
   * Opens a native directory picker.
   * @returns {Promise<string | null>} Absolute path the user selected, or null
   *   if they cancelled.
   */
  selectDirectory: () => ipcRenderer.invoke('select-directory'),

  /**
   * Shows a native desktop notification.
   * @param {string} title
   * @param {string} body
   */
  showNotification: (title, body) =>
    ipcRenderer.invoke('show-notification', String(title), String(body)),

  /**
   * Returns the current app version string (e.g. "0.1.0").
   * @returns {Promise<string>}
   */
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),

  // ── IPC: Update events (renderer subscribes, main.js dispatches) ───────────

  /**
   * Register a callback that fires when a new version is available.
   * @param {(version: string) => void} callback
   * @returns {() => void} Unsubscribe function.
   */
  onUpdateAvailable: (callback) => {
    const handler = (_event, version) => callback(version);
    ipcRenderer.on('update-available', handler);
    return () => ipcRenderer.removeListener('update-available', handler);
  },

  /**
   * Register a callback that fires when the update has been downloaded and is
   * ready to install on next restart.
   * @param {() => void} callback
   * @returns {() => void} Unsubscribe function.
   */
  onUpdateDownloaded: (callback) => {
    const handler = () => callback();
    ipcRenderer.on('update-downloaded', handler);
    return () => ipcRenderer.removeListener('update-downloaded', handler);
  },

  /**
   * Register a callback for download progress (0-100) while an update pulls.
   * @param {(percent: number) => void} callback
   * @returns {() => void} Unsubscribe function.
   */
  onUpdateProgress: (callback) => {
    const handler = (_event, percent) => callback(percent);
    ipcRenderer.on('update-progress', handler);
    return () => ipcRenderer.removeListener('update-progress', handler);
  },

  /**
   * Register a callback for updater errors (download/install failures).
   * @param {(message: string) => void} callback
   * @returns {() => void} Unsubscribe function.
   */
  onUpdateError: (callback) => {
    const handler = (_event, message) => callback(message);
    ipcRenderer.on('update-error', handler);
    return () => ipcRenderer.removeListener('update-error', handler);
  },

  /**
   * Start downloading the available update. Resolves when the download begins;
   * progress arrives via onUpdateProgress, completion via onUpdateDownloaded.
   * @returns {Promise<void>}
   */
  downloadUpdate: () => ipcRenderer.invoke('updater:download'),

  /**
   * Quit and install a downloaded update (relaunches the app).
   * @returns {Promise<void>}
   */
  quitAndInstall: () => ipcRenderer.invoke('updater:quit-and-install'),

  // ── Deep links (potluck:// magic-link sign-in) ─────────────────────────────

  /**
   * Register a callback for potluck:// deep links delivered while the app runs.
   * @param {(url: string) => void} callback
   * @returns {() => void} Unsubscribe function.
   */
  onDeepLink: (callback) => {
    const handler = (_event, url) => callback(url);
    ipcRenderer.on('potluck:deep-link', handler);
    return () => ipcRenderer.removeListener('potluck:deep-link', handler);
  },

  /**
   * Pull a deep link that arrived before the renderer was ready (cold start).
   * Returns the URL once, then null. Calling it marks the renderer ready.
   * @returns {Promise<string | null>}
   */
  getInitialDeepLink: () => ipcRenderer.invoke('potluck:get-initial-deep-link'),

  // ── Mesh sidecar bridge ────────────────────────────────────────────────────
  //
  // The Go mesh sidecar listens on a Unix-domain socket (mac/linux) or named
  // pipe (Windows). Renderer code can't dial those directly, so main.js
  // proxies HTTP requests via the `mesh:request` IPC channel.
  //
  // Method whitelist (enforced in main.js): GET, POST, DELETE.
  // Path must begin with "/". Body is JSON-serialised (or omitted).
  // Return shape: { status: number, body: object | null }
  //   status === 0 indicates a transport-layer failure (sidecar unreachable,
  //   timeout). In that case body.error.code is "sidecar_unreachable" or
  //   "timeout".
  mesh: {
    /**
     * @param {'GET'|'POST'|'DELETE'} method
     * @param {string} path     Must start with "/"
     * @param {unknown} [body]  JSON-serialisable; omit for GET / no-body POST
     * @returns {Promise<{ status: number, body: any }>}
     */
    request: (method, path, body) =>
      ipcRenderer.invoke('mesh:request', method, path, body),
  },
});
