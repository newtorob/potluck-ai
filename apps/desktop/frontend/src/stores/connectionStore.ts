/**
 * Connection store — runtime-editable coordinator URL.
 *
 * Why this exists: the coordinator URL was previously a compile-time
 * constant (`VITE_COORDINATOR_URL` env var with a `http://127.0.0.1:8400`
 * fallback). That broke the multi-machine workflow where users want to
 * point at a coordinator on another machine in their fleet (over LAN
 * or Tailscale) without rebuilding the app.
 *
 * This store persists the URL to localStorage so it survives reloads,
 * and exposes a subscribable value so `useCoordinatorHealth` can re-probe
 * when the user edits the URL.
 *
 * Bootstrap precedence:
 *   1. localStorage value (user-edited)
 *   2. VITE_COORDINATOR_URL build-time env (CI / dev override)
 *   3. http://127.0.0.1:8400 default (matches the docker-compose binding)
 */

import { create } from 'zustand';

const STORAGE_KEY = 'potluck.coordinator_url';
const DEFAULT_URL = 'http://127.0.0.1:8400';

function readBootstrapUrl(): string {
  if (typeof window !== 'undefined') {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored && stored.trim().length > 0) {
      return stored.trim();
    }
  }
  const envUrl = (import.meta.env['VITE_COORDINATOR_URL'] as string | undefined) ?? undefined;
  if (envUrl && envUrl.trim().length > 0) {
    return envUrl.trim();
  }
  return DEFAULT_URL;
}

interface ConnectionState {
  coordinatorUrl: string;
  setCoordinatorUrl: (url: string) => void;
  resetCoordinatorUrl: () => void;
}

export const useConnectionStore = create<ConnectionState>()((set) => ({
  coordinatorUrl: readBootstrapUrl(),

  setCoordinatorUrl(url) {
    const cleaned = url.trim().replace(/\/+$/, ''); // strip trailing slashes
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(STORAGE_KEY, cleaned);
    }
    set({ coordinatorUrl: cleaned });
  },

  resetCoordinatorUrl() {
    if (typeof window !== 'undefined') {
      window.localStorage.removeItem(STORAGE_KEY);
    }
    const envUrl = (import.meta.env['VITE_COORDINATOR_URL'] as string | undefined) ?? undefined;
    set({
      coordinatorUrl: envUrl && envUrl.trim().length > 0 ? envUrl.trim() : DEFAULT_URL,
    });
  },
}));

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
