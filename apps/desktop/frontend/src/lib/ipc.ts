/**
 * Typed wrapper around window.potluck — the context bridge exposed by
 * apps/desktop/electron/preload.js.
 *
 * All methods are safe to call when running outside Electron (e.g. plain
 * browser dev session): they no-op or return sensible defaults rather than
 * throwing, so the React app remains functional for UI development without
 * a running Electron host.
 *
 * See apps/desktop/electron/preload.js for the full bridge definition.
 */

/**
 * window.potluck.updater — the renderer-driven auto-update bridge. All
 * methods/events are optional at the type level: main/preload may not have
 * shipped this yet (or a given platform/build may omit it), and every
 * consumer below degrades to a safe no-op when it's absent.
 */
interface UpdaterBridge {
  check: () => void | Promise<void>;
  download: () => void | Promise<void>;
  install: () => void | Promise<void>;
  onAvailable: (callback: (version: string) => void) => () => void;
  onNotAvailable: (callback: () => void) => () => void;
  onProgress: (callback: (percent: number) => void) => () => void;
  onDownloaded: (callback: () => void) => () => void;
  onError: (callback: (message: string) => void) => () => void;
}

/** Shape written by the Electron main process at build time, if present.
 *  Read defensively everywhere — this may not exist yet. `version` is the
 *  running app's semver, e.g. "0.1.0". */
export interface BuildInfo {
  version: string;
  sha?: string;
  builtAt?: string;
  packaged?: boolean;
  dirty?: boolean;
}

interface PotluckBridge {
  platform: string;
  arch: string;
  versions: {
    electron: string;
    node: string;
  };
  selectDirectory: () => Promise<string | null>;
  showNotification: (title: string, body: string) => Promise<void>;
  getAppVersion: () => Promise<string>;
  onDeepLink?: (callback: (url: string) => void) => () => void;
  getInitialDeepLink?: () => Promise<string | null>;
  updater?: UpdaterBridge;
  app?: { buildInfo?: BuildInfo };
}

/**
 * Bundled fallback for the running app's version, used only when
 * window.potluck.app.buildInfo is absent (a preload that predates
 * buildInfo, or a plain browser dev session). Keep in sync with
 * package.json's "version" — this is a last-resort fallback, never the
 * source of truth.
 */
const BUNDLED_VERSION_FALLBACK = '0.1.0';

/** Safely read the Electron context bridge, or undefined in plain browser */
function getBridge(): PotluckBridge | undefined {
  if (typeof window !== 'undefined' && 'potluck' in window) {
    return (window as unknown as { potluck: PotluckBridge }).potluck;
  }
  return undefined;
}

/**
 * IPC facade. Use these helpers instead of accessing window.potluck directly
 * so TypeScript can enforce the contract and non-Electron environments
 * receive safe defaults.
 */
export const ipc = {
  /** 'darwin' | 'win32' | 'linux', or 'browser' outside Electron */
  get platform(): string {
    return getBridge()?.platform ?? 'browser';
  },

  get arch(): string {
    return getBridge()?.arch ?? 'unknown';
  },

  get versions(): PotluckBridge['versions'] {
    return getBridge()?.versions ?? { electron: 'n/a', node: 'n/a' };
  },

  /** Open a native directory picker; returns the selected path or null */
  async selectDirectory(): Promise<string | null> {
    return getBridge()?.selectDirectory() ?? null;
  },

  /** Show a native OS notification */
  async showNotification(title: string, body: string): Promise<void> {
    return getBridge()?.showNotification(title, body);
  },

  /** Returns the packaged app version from electron-builder */
  async getAppVersion(): Promise<string> {
    return (await getBridge()?.getAppVersion()) ?? '0.0.0-dev';
  },

  /** True when the app is running inside an Electron window */
  get isElectron(): boolean {
    return getBridge() !== undefined;
  },

  /**
   * Subscribe to potluck:// deep links delivered while the app is running.
   * No-ops (returns a no-op unsubscribe) outside Electron.
   */
  onDeepLink(callback: (url: string) => void): () => void {
    const bridge = getBridge();
    if (!bridge?.onDeepLink) return () => {};
    return bridge.onDeepLink(callback);
  },

  /**
   * Pull a deep link that arrived before the renderer mounted (cold start).
   * Returns null outside Electron or when there is none.
   */
  async getInitialDeepLink(): Promise<string | null> {
    return (await getBridge()?.getInitialDeepLink?.()) ?? null;
  },

  // ── Auto-update (renderer-driven check/download/install) ──────────────────
  // All no-op safely outside Electron, or when main/preload hasn't shipped
  // window.potluck.updater yet — so the update UI never throws, it just
  // never has anything to show.

  /** The build stamp written by the main process, if present. */
  get buildInfo(): BuildInfo | undefined {
    return getBridge()?.app?.buildInfo;
  },

  /** The running app's semver — buildInfo.version, or the bundled fallback. */
  get runningVersion(): string {
    return getBridge()?.app?.buildInfo?.version ?? BUNDLED_VERSION_FALLBACK;
  },

  /** True only when window.potluck.updater is actually present. */
  get hasUpdater(): boolean {
    return getBridge()?.updater !== undefined;
  },

  /** Ask the main process to check for an update. Safe to call speculatively. */
  async checkForUpdate(): Promise<void> {
    await getBridge()?.updater?.check();
  },

  /** Begin downloading the available update. */
  async downloadUpdate(): Promise<void> {
    await getBridge()?.updater?.download();
  },

  /** Install the downloaded update (relaunches the app). */
  async installUpdate(): Promise<void> {
    await getBridge()?.updater?.install();
  },

  /** Fires when a newer version is published. Returns an unsubscribe fn. */
  onUpdateAvailable(callback: (version: string) => void): () => void {
    const updater = getBridge()?.updater;
    if (!updater) return () => {};
    return updater.onAvailable(callback);
  },

  /** Fires when a check completes and the running version is already current. */
  onUpdateNotAvailable(callback: () => void): () => void {
    const updater = getBridge()?.updater;
    if (!updater) return () => {};
    return updater.onNotAvailable(callback);
  },

  /** Fires with download progress (0-100) while the update pulls. */
  onUpdateProgress(callback: (percent: number) => void): () => void {
    const updater = getBridge()?.updater;
    if (!updater) return () => {};
    return updater.onProgress(callback);
  },

  /** Fires when the update finished downloading and is ready to install. */
  onUpdateDownloaded(callback: () => void): () => void {
    const updater = getBridge()?.updater;
    if (!updater) return () => {};
    return updater.onDownloaded(callback);
  },

  /** Fires when a check/download/install error occurs. */
  onUpdateError(callback: (message: string) => void): () => void {
    const updater = getBridge()?.updater;
    if (!updater) return () => {};
    return updater.onError(callback);
  },
};
