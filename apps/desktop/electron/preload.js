'use strict';

/**
 * Preload script - runs in the renderer process before any page JS loads.
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

  // ── Build stamp (staleness detection) ──────────────────────────────────────
  //
  // Static value, not a function - computed once in main.js at startup (see
  // main.js's loadBuildInfo()) and read here via a synchronous IPC call so it
  // is available immediately, with no await, for a "you're running build X"
  // badge. Shape: { version, sha, builtAt, packaged, dirty? }. `version` is
  // app.getVersion() (e.g. "0.1.0") - for comparing against the server's
  // minimum/latest version to detect a stale client.
  app: {
    buildInfo: ipcRenderer.sendSync('app:get-build-info-sync'),
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

  // ── Updater (namespaced) ────────────────────────────────────────────────────
  //
  // Same underlying electron-updater flow as the flat onUpdate*/downloadUpdate/
  // quitAndInstall members above (kept for back-compat) - this is the
  // updater-focused surface: window.potluck.updater.{check,download,install}
  // plus on*() subscriptions that mirror the update:* / update-* IPC channels
  // main.js registers. autoDownload is off in main.js, so check() only ever
  // resolves an available/not-available event; download() and install() are
  // explicit user actions from the UI (e.g. a "Download update" / "Restart to
  // update" banner).
  updater: {
    /**
     * Ask main to check the update feed now. In dev (unpackaged) this
     * resolves { status: 'dev' } rather than throwing - there is no
     * published release to diff against outside a packaged build.
     * @returns {Promise<{ status: 'ok' | 'dev' | 'error', message?: string }>}
     */
    check: () => ipcRenderer.invoke('update:check'),

    /**
     * Start downloading the update main already found. Progress arrives via
     * onProgress, completion via onDownloaded.
     * @returns {Promise<{ status: 'ok' | 'dev' | 'error', message?: string }>}
     */
    download: () => ipcRenderer.invoke('update:download'),

    /**
     * Quit and install a downloaded update (relaunches the app).
     * @returns {Promise<{ status: 'ok' | 'dev' }>}
     */
    install: () => ipcRenderer.invoke('update:install'),

    /**
     * Register a callback that fires when a new version is available.
     * @param {(version: string) => void} callback
     * @returns {() => void} Unsubscribe function.
     */
    onAvailable: (callback) => {
      const handler = (_event, version) => callback(version);
      ipcRenderer.on('update-available', handler);
      return () => ipcRenderer.removeListener('update-available', handler);
    },

    /**
     * Register a callback that fires when a check comes back with no update
     * (i.e. the app is already current).
     * @param {() => void} callback
     * @returns {() => void} Unsubscribe function.
     */
    onNotAvailable: (callback) => {
      const handler = () => callback();
      ipcRenderer.on('update-not-available', handler);
      return () => ipcRenderer.removeListener('update-not-available', handler);
    },

    /**
     * Register a callback for download progress (0-100) while an update pulls.
     * @param {(percent: number) => void} callback
     * @returns {() => void} Unsubscribe function.
     */
    onProgress: (callback) => {
      const handler = (_event, percent) => callback(percent);
      ipcRenderer.on('update-progress', handler);
      return () => ipcRenderer.removeListener('update-progress', handler);
    },

    /**
     * Register a callback that fires when the update has been downloaded and
     * is ready to install on next restart.
     * @param {() => void} callback
     * @returns {() => void} Unsubscribe function.
     */
    onDownloaded: (callback) => {
      const handler = () => callback();
      ipcRenderer.on('update-downloaded', handler);
      return () => ipcRenderer.removeListener('update-downloaded', handler);
    },

    /**
     * Register a callback for updater errors (check/download/install failures).
     * @param {(message: string) => void} callback
     * @returns {() => void} Unsubscribe function.
     */
    onError: (callback) => {
      const handler = (_event, message) => callback(message);
      ipcRenderer.on('update-error', handler);
      return () => ipcRenderer.removeListener('update-error', handler);
    },
  },

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

  // ── Cloud API bridge (packaged app only; bypasses file:// CORS) ─────────────
  //
  // Proxies account/membership/billing calls to api.trypotluck.ai through the
  // main process. The session token is held in main and attached as a bearer
  // credential there; the renderer never sees or stores it.
  cloud: {
    /** Proxy a cloud call. Returns { status, body }. */
    request: (method, path, body) =>
      ipcRenderer.invoke('cloud:request', method, path, body),
    /** Persist the opaque session token in the main process. */
    setToken: (token) => ipcRenderer.invoke('cloud:set-token', String(token)),
    /** Forget the session token (sign-out, or an expired/invalid token). */
    clearToken: () => ipcRenderer.invoke('cloud:clear-token'),
    /**
     * Whether a cloud session token is currently persisted, without making a
     * network call. Used by the sign-in wall (AuthGate) to tell "signed out
     * because offline" (token present, /me just couldn't be reached) apart
     * from "signed out because there's no account" (no token at all).
     * @returns {Promise<boolean>}
     */
    hasToken: () => ipcRenderer.invoke('cloud:has-token'),
    /**
     * Proxy a session-authenticated call to the (user-configured) coordinator.
     * Attaches the same bearer token as `request` above, but against a
     * caller-supplied coordinator origin rather than the fixed cloud API host
     * - main.js allowlists the exact paths this may target. Returns
     * { status, body }.
     * @param {string} baseUrl  Coordinator origin, e.g. from connectionStore.
     * @param {'POST'} method
     * @param {string} path     Must be one of the allowlisted session-auth paths.
     */
    coordinatorRequest: (baseUrl, method, path, body) =>
      ipcRenderer.invoke('coordinator:session-request', baseUrl, method, path, body),
    /**
     * Tells main which coordinator origin the renderer is currently pointed
     * at (connectionStore.ts's coordinatorUrl), so main's
     * onBeforeSendHeaders injector can attach the member session bearer to
     * renderer→coordinator requests that never go through an IPC bridge -
     * e.g. streamNetworkJob's SSE POST to /jobs/stream, which can't be
     * buffered through coordinatorRequest above (that reads the whole
     * response body; a live stream can't cross that boundary). Call once at
     * startup and again whenever coordinatorUrl changes.
     * @param {string} baseUrl
     * @returns {Promise<void>}
     */
    setCoordinatorBase: (baseUrl) =>
      ipcRenderer.invoke('coordinator:set-base', String(baseUrl)),
    /**
     * Ask main to hand the member session to the local engine again. For a
     * circle chat the engine refused with 401 because it holds no session
     * (it restarted, or never got one). No token goes in or comes out.
     * @returns {Promise<boolean>} true when the engine now has a session.
     */
    resyncEngineSession: () => ipcRenderer.invoke('cloud:resync-engine-session'),
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

    // ── Mesh daemon supervision ("app brings up the mesh") ──────────────────
    //
    // Distinct from `request` above: these three drive whether the mesh
    // sidecar is even running as a system daemon, not what it does once it
    // is. macOS: backed by SMAppService via a signed helper CLI. Linux:
    // a root systemd unit installed via a polkit/pkexec helper. Windows: a
    // SYSTEM-run Scheduled Task installed via a single UAC elevation prompt
    // (Start-Process -Verb RunAs against a fixed, app-shipped PowerShell
    // helper) - see main.js's mesh-daemon-supervision sections for each
    // platform's exact mechanism.
    //
    // `state` values: 'not_installed' | 'requires_approval' | 'starting' |
    // 'running' | 'outdated' | 'failed' | 'unsupported'. Not every platform can produce
    // every value (e.g. only macOS reports 'requires_approval'; Windows and
    // Linux never do - see main.js for which states each platform's
    // compute*DaemonStatus() can return).
    //
    // 'outdated' means the service is up but is not the sidecar binary this
    // app shipped (an app update left the old process running). enable()
    // restarts it.

    /**
     * Current mesh daemon status. Safe to poll (e.g. every 2-5s from a
     * settings/status view) - each call is one short helper subprocess plus,
     * on macOS when the daemon reports enabled, one local socket probe.
     * @returns {Promise<{ platform: string, state: string, detail?: string }>}
     */
    daemonStatus: () => ipcRenderer.invoke('mesh:daemon-status'),

    /**
     * Registers (or re-registers) the mesh daemon. May resolve with
     * state: 'requires_approval' - that is a SUCCESSFUL outcome, not an
     * error: the daemon is registered but the user must approve it in
     * System Settings > Login Items & Extensions before it starts. Follow up
     * with openLoginItems() to take them straight there.
     *
     * When daemonStatus() says 'outdated' this restarts the service instead.
     * Linux/Windows: the same elevated install, which ends with a service
     * restart. macOS: the registration is refreshed (unregister, then
     * register), then the running service is asked to restart, or, if it is
     * too old to have that command, the system's administrator dialog is
     * shown to stop it. See main.js, "Mesh service version check".
     * @returns {Promise<{ platform: string, state: string, detail?: string }>}
     */
    enable: () => ipcRenderer.invoke('mesh:enable'),

    /**
     * Opens System Settings > General > Login Items & Extensions so the user
     * can approve the already-registered mesh daemon.
     * @returns {Promise<{ platform: string, ok: boolean }>}
     */
    openLoginItems: () => ipcRenderer.invoke('mesh:open-login-items'),
  },
});
