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
