/**
 * Settings store - mirrors the Python sidecar's PotluckSettings shape.
 *
 * Mutations optimistically apply changes to the local store, then
 * call PATCH /settings. On error, the previous value is restored and
 * saveError is set so the UI can show feedback.
 *
 * Theme is applied to <html> as a class ("dark" / "light") by useSettings
 * so Tailwind's darkMode: 'class' picks it up.
 */

import { create } from 'zustand';
import { localApi } from '@/lib/localApi';
import type { PotluckSettings } from '@/lib/localApi';

// ---------------------------------------------------------------------------
// Deep-partial helper for the patch type
// ---------------------------------------------------------------------------

type DeepPartial<T> = T extends object ? { [K in keyof T]?: DeepPartial<T[K]> } : T;

interface SettingsState {
  settings: PotluckSettings | null;
  isLoaded: boolean;
  isLoading: boolean;
  loadError: string | null;
  isSaving: boolean;
  saveError: string | null;

  // Actions
  fetch: () => Promise<void>;
  update: (patch: DeepPartial<PotluckSettings>) => Promise<void>;
  /** Seed the store from a GET /settings response someone else already made
   *  (durablePrefs at App mount). Lets gates that read settings - notably
   *  privacy.chat_history_enabled - have a real value before the first send,
   *  with no extra round-trip. Seed-only: a late startup response must not
   *  replace settings already fetched or changed in this session. */
  hydrateFromSidecar: (settings: PotluckSettings) => void;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useSettingsStore = create<SettingsState>()((set, get) => {
  let revision = 0;
  let inFlight: Promise<void> | null = null;
  let pendingUpdates = 0;
  const invalidateRead = () => {
    revision++;
    inFlight = null;
  };

  return {
    settings: null,
    isLoaded: false,
    isLoading: false,
    loadError: null,
    isSaving: false,
    saveError: null,

    fetch() {
      if (inFlight) return inFlight;
      const startedAt = ++revision;
      set({ isLoading: true });
      // Preserve an existing error while retrying so the retry control stays
      // visible. A delayed read cannot undo hydration or an optimistic/save result.
      let response: Promise<PotluckSettings>;
      try {
        response = Promise.resolve(localApi.getSettings());
      } catch (error) {
        response = Promise.reject(error);
      }
      inFlight = response
        .then((settings) => {
          if (revision === startedAt && pendingUpdates === 0) {
            set({ settings, isLoaded: true, loadError: null });
          }
        })
        .catch(() => {
          if (revision === startedAt && pendingUpdates === 0) {
            set({
              loadError:
                'Could not load settings. Try again, or close and reopen Potluck if the problem continues.',
            });
          }
        })
        .finally(() => {
          if (revision === startedAt) {
            inFlight = null;
            set({ isLoading: false });
          }
        });
      return inFlight;
    },

    async update(patch) {
      const previous = get().settings;
      if (!previous) return;

      // Optimistic update - deep-merge the patch
      const optimistic = deepMerge(previous, patch);
      pendingUpdates++;
      invalidateRead();
      set({ settings: optimistic, isSaving: true, saveError: null, isLoading: false });

      try {
        const updated = await localApi.updateSettings(patch as Partial<PotluckSettings>);
        invalidateRead();
        set({ settings: updated, isSaving: false, isLoading: false, loadError: null });
      } catch (err) {
        // Rollback to previous on failure
        invalidateRead();
        set({
          settings: previous,
          isSaving: false,
          isLoading: false,
          saveError: err instanceof Error ? err.message : 'Failed to save settings',
        });
        throw err;
      } finally {
        pendingUpdates--;
      }
    },

    hydrateFromSidecar(settings) {
      if (get().settings !== null) return;
      invalidateRead();
      set({ settings, isLoaded: true, isLoading: false, loadError: null });
    },
  };
});

// ---------------------------------------------------------------------------
// Deep merge utility (no external dep needed)
// ---------------------------------------------------------------------------

function deepMerge<T extends object>(target: T, source: DeepPartial<T>): T {
  // Cast both sides to Record<string, unknown> - the result is re-cast to T
  // at the call site. We avoid per-key generic narrowing which TS rejects under
  // exactOptionalPropertyTypes when iterating unknown generic keys.
  const tgt = target as Record<string, unknown>;
  const src = source as Record<string, unknown>;
  const result: Record<string, unknown> = { ...tgt };

  for (const key of Object.keys(src)) {
    const srcVal = src[key];
    const tgtVal = tgt[key];
    if (
      srcVal !== undefined &&
      srcVal !== null &&
      typeof srcVal === 'object' &&
      !Array.isArray(srcVal) &&
      tgtVal !== undefined &&
      tgtVal !== null &&
      typeof tgtVal === 'object'
    ) {
      result[key] = deepMerge(tgtVal, srcVal);
    } else if (srcVal !== undefined) {
      result[key] = srcVal;
    }
  }

  return result as T;
}
