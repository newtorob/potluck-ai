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
}

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
};
