/**
 * Connection store — runtime-editable coordinator URL.
 *
 * Why this exists: the coordinator URL was previously a compile-time
 * constant (`VITE_COORDINATOR_URL` env var with a `http://127.0.0.1:8400`
 * fallback). That broke the multi-machine workflow where users want to
 * point at a coordinator on another machine in their fleet (over LAN
 * or Tailscale) without rebuilding the app.
 *
 * This store persists explicit user overrides to BOTH localStorage and the
 * Python sidecar's durable config (~/.potluck/config.json →
 * network.coordinator_url). On startup, App.tsx reads the durable config
 * and calls `hydrateFromSidecar()` so the setting survives localStorage
 * wipes, reinstalls, and cross-surface launches (dev vs packaged app).
 *
 * Bootstrap + runtime precedence:
 *   1. Explicit user override — a value the user typed + saved. Written to
 *      localStorage (fast-path cache) AND the Python sidecar's durable
 *      config. Wins over everything. Restored from the durable config on
 *      startup if localStorage was wiped.
 *   2. Sidecar-reported URL — `coordinator_url` from the Go mesh sidecar's
 *      live /status response. Adopted via `adoptSidecarUrl()` only when
 *      there is no explicit override. NOT written to localStorage or the
 *      durable config — it is a transient, lower-precedence signal.
 *   3. VITE_COORDINATOR_URL build-time env (CI / dev override).
 *   4. http://127.0.0.1:8400 default (matches the docker-compose binding).
 *
 * Write-through contract:
 *   `setCoordinatorUrl()` and `resetCoordinatorUrl()` write to localStorage
 *   first (so the session is correct even if the sidecar is not yet ready),
 *   then fire-and-forget a PATCH /settings to the Python sidecar so the
 *   value is durable across restarts and localhost wipes.
 *
 *   `adoptSidecarUrl()` updates `coordinatorUrl` in memory and sets
 *   `sidecarReportedUrl` but does NOT touch localStorage or the durable
 *   config. It is the lower-precedence mesh-status signal; conflating it
 *   with the user's durable override would corrupt the config on reset.
 */

import { create } from 'zustand';

const STORAGE_KEY = 'potluck.coordinator_url';
const DEFAULT_URL = 'http://127.0.0.1:8400';

// Python sidecar base — same constant as localApi.ts, duplicated here to
// avoid a circular import (localApi.ts imports coordinatorBase from this
// module). Only used inside function bodies (after both modules are fully
// evaluated), so the cross-module ordering is safe.
const LOCAL_SIDECAR_BASE =
  (import.meta.env['VITE_LOCAL_API_URL'] as string | undefined) ?? 'http://127.0.0.1:8321';

/**
 * Fire-and-forget write of the coordinator URL to the Python sidecar's
 * durable config (~/.potluck/config.json → network.coordinator_url).
 *
 * Callers: `setCoordinatorUrl` (writes the url) and `resetCoordinatorUrl`
 * (writes "" to signal "no explicit override").
 *
 * The localStorage write always happens first in the caller, so even when
 * the sidecar is unreachable the session is correct for this launch.
 */
function persistToSidecar(url: string): void {
  void fetch(`${LOCAL_SIDECAR_BASE}/settings`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ network: { coordinator_url: url } }),
  }).catch((err: unknown) => {
    console.warn('[connectionStore] coordinator URL sync to sidecar failed:', err);
  });
}

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
   * save a coordinator URL. Persists to localStorage (fast-path cache) AND
   * fires a durable write to the Python sidecar config so the value
   * survives localStorage wipes and reinstalls. Marks the override explicit.
   * This value wins over the sidecar-reported URL.
   */
  setCoordinatorUrl: (url: string) => void;

  /**
   * Clear the explicit user override and fall back through the precedence
   * chain: sidecarReportedUrl -> env -> default.
   * Clears localStorage and writes "" to the Python sidecar config so the
   * next cold-start does not re-adopt a stale override.
   */
  resetCoordinatorUrl: () => void;

  /**
   * Called by meshStore whenever a /status response arrives. Updates
   * `sidecarReportedUrl` always, and — if there is no explicit user
   * override — also updates `coordinatorUrl` so all consumers see the
   * sidecar's actual target. Does NOT write to localStorage or the durable
   * sidecar config (lower-precedence transient signal only).
   */
  adoptSidecarUrl: (url: string) => void;

  /**
   * Called once on app mount after reading the Python sidecar's durable
   * settings (GET /settings → network.coordinator_url). When `url` is
   * non-empty, adopts it as the explicit override and syncs it to
   * localStorage so the fast-path cache matches the durable store. No-op
   * when `url` is empty (preserves whatever localStorage / env / default
   * provided at init). The durable config wins over a stale localStorage
   * value when both are present.
   */
  hydrateFromSidecar: (url: string) => void;
}

export const useConnectionStore = create<ConnectionState>()((set, get) => {
  const storedOverride = readStoredOverride();

  return {
    coordinatorUrl: storedOverride ?? staticDefault(),
    hasExplicitOverride: storedOverride !== null,
    sidecarReportedUrl: null,

    setCoordinatorUrl(url) {
      const cleaned = url.trim().replace(/\/+$/, '');
      // localStorage first: the session is correct even if the sidecar is
      // not yet ready or is temporarily unreachable.
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(STORAGE_KEY, cleaned);
      }
      set({ coordinatorUrl: cleaned, hasExplicitOverride: true });
      // Durable write: survives localStorage wipe, reinstall, cross-surface
      // launch. Fire-and-forget — localStorage already guards this session.
      persistToSidecar(cleaned);
    },

    resetCoordinatorUrl() {
      if (typeof window !== 'undefined') {
        window.localStorage.removeItem(STORAGE_KEY);
      }
      // Write "" so the next cold-start hydration sees "no override" rather
      // than re-adopting whatever was last written. Fire-and-forget.
      persistToSidecar('');
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

    hydrateFromSidecar(url) {
      const cleaned = url.trim().replace(/\/+$/, '');
      if (!cleaned) return; // empty = no durable override; keep current state
      // Durable config wins over any stale localStorage value (write-through
      // keeps them in sync; a mismatch means localStorage was wiped).
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(STORAGE_KEY, cleaned);
      }
      set({ coordinatorUrl: cleaned, hasExplicitOverride: true });
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

/**
 * True when the app's active coordinatorUrl disagrees with what this
 * machine's mesh daemon actually reports itself connected to
 * (sidecarReportedUrl — populated from the Go mesh daemon's own live
 * GET /status via meshStore's adoptSidecarUrl calls, NOT the Python
 * sidecar despite the name inherited from that field).
 *
 * Per this file's precedence, `adoptSidecarUrl` keeps `coordinatorUrl` and
 * `sidecarReportedUrl` in sync whenever there's no explicit user override —
 * so a divergence here can only mean there IS an explicit override, and
 * it's stale relative to what the daemon is actually dialing (e.g. a
 * coordinator URL saved during dev testing, still active in a prod
 * session). This is exactly the "app knew the right answer and didn't use
 * it" failure mode: the daemon is on the right coordinator, but
 * session-authed calls (issuePairingToken, household setup, pairing) go
 * through the app's own `coordinatorUrl` and get rejected by a coordinator
 * that never saw the user's session.
 *
 * `sidecarReportedUrl === null` (no daemon status yet — not started, dev
 * without the bridge, or a platform without daemon supervision) is never a
 * mismatch; there's nothing to compare against.
 */
export function isCoordinatorMismatched(
  coordinatorUrl: string,
  sidecarReportedUrl: string | null,
): boolean {
  return sidecarReportedUrl !== null && sidecarReportedUrl !== coordinatorUrl;
}
