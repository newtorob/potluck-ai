/**
 * updateStore — shared state for the whole "stale client can never happen"
 * flow. Two independent signals feed the same phase machine:
 *
 *   1. The local auto-updater (electron-updater via window.potluck.updater):
 *      onAvailable/onProgress/onDownloaded/onError events, driven by
 *      UpdateBanner's subscription. Task 1.
 *   2. The cloud version handshake (GET /client/version): a semver compare
 *      against `minimum` (blocking) and `latest` (soft nudge). Task 2.
 *
 * They share one store so the handshake can drive the *same* "available"
 * banner the local updater would show (`checkVersionHandshake` calls
 * `setAvailable` when nothing else is already in flight), and so
 * Settings' "Check for updates" status line and the top banner never
 * disagree.
 *
 * Everything here is defensive by construction: ipc.* and cloudApi.* already
 * no-op / fail safely when window.potluck (or its `updater` sub-bridge) or
 * the cloud endpoint aren't present yet — this store never assumes either
 * exists.
 */

import { create } from 'zustand';
import { ipc } from '@/lib/ipc';
import { cloudApi } from '@/lib/cloudApi';
import { isVersionBelow } from '@/lib/semver';

export type UpdatePhase = 'idle' | 'available' | 'downloading' | 'ready' | 'error';

interface UpdateState {
  // ── Local auto-updater state (Task 1) ──────────────────────────────────
  phase: UpdatePhase;
  /** The version the current phase refers to (available/ready), if known. */
  version: string | null;
  percent: number;
  errorMessage: string | null;
  /** User dismissed the soft "available" banner. Reset whenever a new
   *  version becomes available so a later nudge isn't suppressed forever. */
  dismissed: boolean;

  // ── Cloud version handshake state (Task 2) ─────────────────────────────
  minimumVersion: string | null;
  latestVersion: string | null;
  notesUrl: string | null;
  /** True when the running version is below `minimum` — blocks the app. */
  blocked: boolean;
  /** True once the handshake has settled (success, absence, or failure). */
  versionCheckDone: boolean;

  setAvailable: (version: string) => void;
  setNotAvailable: () => void;
  setProgress: (percent: number) => void;
  setDownloaded: () => void;
  setError: (message: string) => void;
  dismiss: () => void;

  /** Ask the main process to check for an update (fire-and-forget). */
  check: () => void;
  /** Begin downloading the available update. */
  download: () => void;
  /** Install the downloaded update / restart. */
  install: () => void;

  /** GET /client/version, compare against the running version, set
   *  `blocked` and (optionally) nudge `phase` to 'available'. Never throws —
   *  any failure (404, offline, malformed body) leaves state untouched
   *  beyond marking the check done. */
  checkVersionHandshake: () => Promise<void>;
}

export const useUpdateStore = create<UpdateState>()((set, get) => ({
  phase: 'idle',
  version: null,
  percent: 0,
  errorMessage: null,
  dismissed: false,

  minimumVersion: null,
  latestVersion: null,
  notesUrl: null,
  blocked: false,
  versionCheckDone: false,

  setAvailable(version) {
    set({ phase: 'available', version, dismissed: false, errorMessage: null });
  },

  setNotAvailable() {
    // A fresh "up to date" result clears a stale error/idle state, but never
    // clobbers an in-flight download or a ready-to-restart state.
    set((s) => (s.phase === 'error' || s.phase === 'idle' ? { phase: 'idle', errorMessage: null } : s));
  },

  setProgress(percent) {
    set({ phase: 'downloading', percent });
  },

  setDownloaded() {
    set({ phase: 'ready', percent: 100 });
  },

  setError(message) {
    set({ phase: 'error', errorMessage: message });
  },

  dismiss() {
    set({ dismissed: true });
  },

  check() {
    void ipc.checkForUpdate();
  },

  download() {
    set({ phase: 'downloading', percent: 0 });
    void ipc.downloadUpdate();
  },

  install() {
    void ipc.installUpdate();
  },

  async checkVersionHandshake() {
    try {
      const info = await cloudApi.getClientVersion();
      if (!info || typeof info.minimum !== 'string' || typeof info.latest !== 'string') {
        set({ versionCheckDone: true });
        return;
      }
      const running = ipc.runningVersion;
      const blocked = isVersionBelow(running, info.minimum);
      set({
        minimumVersion: info.minimum,
        latestVersion: info.latest,
        notesUrl: info.notesUrl ?? null,
        blocked,
        versionCheckDone: true,
      });
      // Soft nudge: reuse the same "available" banner phase the local
      // auto-updater drives, but only when nothing is already in flight.
      if (!blocked && isVersionBelow(running, info.latest) && get().phase === 'idle') {
        get().setAvailable(info.latest);
      }
    } catch {
      // 404 (not deployed yet), offline, malformed body — all the same:
      // no version info, never block on it.
      set({ versionCheckDone: true });
    }
  },
}));
