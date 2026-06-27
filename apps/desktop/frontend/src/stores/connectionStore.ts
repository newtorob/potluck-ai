/**
 * Connection store — runtime-editable coordinator URL.
 *
 * Why this exists: the coordinator URL was previously a compile-time
 * constant (`VITE_COORDINATOR_URL` env var with a `http://127.0.0.1:8400`
 * fallback). That broke the multi-machine workflow where users want to
 * point at a coordinator on another machine in their fleet (over LAN
 * or Tailscale) without rebuilding the app.
 *
 * This store persists explicit user overrides to localStorage and exposes a
 * subscribable value so `useCoordinatorHealth` can re-probe when the URL
 * changes.
 *
 * Bootstrap + runtime precedence:
 *   1. Explicit user override — a value the user typed + saved. Stored in
 *      localStorage under 'potluck.coordinator_url'. Wins over everything.
 *   2. Sidecar-reported URL — `coordinator_url` from the mesh sidecar's
 *      live /status response. Adopted via `adoptSidecarUrl()` only when
 *      there is no explicit override. NOT written to localStorage so it
 *      is never confused with an override on the next boot.
 *   3. VITE_COORDINATOR_URL build-time env (CI / dev override).
 *   4. http://127.0.0.1:8400 default (matches the docker-compose binding).
 *
 * Anti-feedback-loop contract:
 *   `adoptSidecarUrl()` updates `coordinatorUrl` in memory and sets
 *   `sidecarReportedUrl` but does NOT touch localStorage and does NOT
 *   flip `hasExplicitOverride`. App.tsx's `updateSettings` push runs
 *   whenever `coordinatorUrl` changes — that writes to the Python sidecar,
 *   not back to the Go mesh sidecar's /status, so there is no cycle.
 */

import { create } from 'zustand';

const STORAGE_KEY = 'potluck.coordinator_url';
const DEFAULT_URL = 'http://127.0.0.1:8400';

function envUrl(): string | undefined {
  const v = import.meta.env.VITE_COORDINATOR_URL;
  return v && v.trim().length > 0 ? v.trim() : undefined;
}

/** The fallback when there is no override and no sidecar report yet. */
function staticDefault(): string {
  return envUrl() ?? DEFAULT_URL;
}

/** Read the explicit user override from localStorage, if one was saved. */
function readStoredOverride(): string | null {
  if (typeof window === 'undefined') return null;
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored && stored.trim().length > 0 ? stored.trim() : null;
}

interface ConnectionState {
  /** The coordinator URL currently in use — read by all consumers. */
  coordinatorUrl: string;

  /**
   * True when the user has explicitly saved a URL via `setCoordinatorUrl`.
   * False when we are using the sidecar-reported URL or the env/default
   * fallback. Only explicit overrides are persisted to localStorage.
   */
  hasExplicitOverride: boolean;

  /**
   * The last URL the mesh sidecar reported via its /status response.
   * Null until the first successful status poll.
   */
  sidecarReportedUrl: string | null;

  /**
   * Called by the user (via CooperativeConnectionPanel) when they type and
   * save a coordinator URL. Persists to localStorage and marks the override
   * as explicit. This value wins over the sidecar-reported URL.
   */
  setCoordinatorUrl: (url: string) => void;

  /**
   * Clear the explicit user override and fall back through the precedence
   * chain: sidecarReportedUrl -> env -> default.
   */
  resetCoordinatorUrl: () => void;

  /**
   * Called by meshStore whenever a /status response arrives. Updates
   * `sidecarReportedUrl` always, and — if there is no explicit user
   * override — also updates `coordinatorUrl` so all consumers see the
   * sidecar's actual target. Does NOT write to localStorage.
   */
  adoptSidecarUrl: (url: string) => void;
}

export const useConnectionStore = create<ConnectionState>()((set, get) => {
  const storedOverride = readStoredOverride();

  return {
    coordinatorUrl: storedOverride ?? staticDefault(),
    hasExplicitOverride: storedOverride !== null,
    sidecarReportedUrl: null,

    setCoordinatorUrl(url) {
      const cleaned = url.trim().replace(/\/+$/, '');
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(STORAGE_KEY, cleaned);
      }
      set({ coordinatorUrl: cleaned, hasExplicitOverride: true });
    },

    resetCoordinatorUrl() {
      if (typeof window !== 'undefined') {
        window.localStorage.removeItem(STORAGE_KEY);
      }
      // Fall back through the chain: sidecar report -> env -> default.
      const { sidecarReportedUrl } = get();
      const fallback = sidecarReportedUrl ?? staticDefault();
      set({ coordinatorUrl: fallback, hasExplicitOverride: false });
    },

    adoptSidecarUrl(url) {
      const cleaned = url.trim().replace(/\/+$/, '');
      const { hasExplicitOverride } = get();
      if (hasExplicitOverride) {
        // Record what the sidecar says but keep the user's choice active.
        set({ sidecarReportedUrl: cleaned });
      } else {
        // No user override — use the sidecar's actual target.
        set({ coordinatorUrl: cleaned, sidecarReportedUrl: cleaned });
      }
    },
  };
});

/**
 * Imperative accessor for non-React code (the API client).
 *
 * Use this from inside fetch URLs:
 *
 *     fetch(`${coordinatorBase()}/health`)
 *
 * Do NOT cache the result across calls — the value is mutable.
 */
export function coordinatorBase(): string {
  return useConnectionStore.getState().coordinatorUrl;
}
