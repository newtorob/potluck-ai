/**
 * Settings store — mirrors the Python sidecar's PotluckSettings shape.
 *
 * Mutations optimistically apply changes to the local store, then
 * call PATCH /settings. On error, the previous value is restored and
 * saveError is set so the UI can show feedback.
 *
 * Theme is applied to <html> as a class ("dark" / "light") by useSettings
 * so Tailwind's darkMode: 'class' picks it up.
 *
 * See guide §4.4.
 */

import { create } from 'zustand';
import { localApi } from '@/lib/localApi';
import type { PotluckSettings } from '@/lib/localApi';

// ---------------------------------------------------------------------------
// Deep-partial helper for the patch type
// ---------------------------------------------------------------------------

type DeepPartial<T> = T extends object
  ? { [K in keyof T]?: DeepPartial<T[K]> }
  : T;

interface SettingsState {
  settings: PotluckSettings | null;
  isLoaded: boolean;
  isSaving: boolean;
  saveError: string | null;

  // Actions
  fetch: () => Promise<void>;
  update: (patch: DeepPartial<PotluckSettings>) => Promise<void>;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useSettingsStore = create<SettingsState>()((set, get) => ({
  settings: null,
  isLoaded: false,
  isSaving: false,
  saveError: null,

  async fetch() {
    try {
      const settings = await localApi.getSettings();
      set({ settings, isLoaded: true });
    } catch (err) {
      // Sidecar may not be running during pure UI development — log and continue
      console.warn('[settingsStore] Could not load settings:', err);
    }
  },

  async update(patch) {
    const previous = get().settings;
    if (!previous) return;

    // Optimistic update — deep-merge the patch
    const optimistic = deepMerge(previous, patch);
    set({ settings: optimistic, isSaving: true, saveError: null });

    try {
      const updated = await localApi.updateSettings(patch as Partial<PotluckSettings>);
      set({ settings: updated, isSaving: false });
    } catch (err) {
      // Rollback to previous on failure
      set({
        settings: previous,
        isSaving: false,
        saveError: err instanceof Error ? err.message : 'Failed to save settings',
      });
      throw err;
    }
  },
}));

// ---------------------------------------------------------------------------
// Deep merge utility (no external dep needed)
// ---------------------------------------------------------------------------

function deepMerge<T extends object>(target: T, source: DeepPartial<T>): T {
  // Cast both sides to Record<string, unknown> — the result is re-cast to T
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
