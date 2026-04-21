/**
 * Settings store — mirrors the Python sidecar's PotluckSettings shape.
 *
 * Populated by useSettings hook on mount. Mutations call
 * localApi.updateSettings() and then sync the store optimistically.
 *
 * Theme is applied to <html> as a class ("dark" / "light") by useSettings
 * so Tailwind's darkMode: 'class' picks it up.
 *
 * See guide §4.4 and apps/desktop/python/potluck_local/services/settings_manager.py.
 */

import { create } from 'zustand';
import type {
  RuntimeSettings,
  PrivacySettings,
  NetworkSettings,
  AppSettingsShape,
  PotluckSettings,
} from '@/lib/localApi';

interface SettingsState {
  settings: PotluckSettings | null;
  isLoaded: boolean;
  isSaving: boolean;
  saveError: string | null;

  // Actions
  setSettings: (settings: PotluckSettings) => void;
  patchRuntime: (patch: Partial<RuntimeSettings>) => void;
  patchPrivacy: (patch: Partial<PrivacySettings>) => void;
  patchNetwork: (patch: Partial<NetworkSettings>) => void;
  patchApp: (patch: Partial<AppSettingsShape>) => void;
  setSaving: (saving: boolean) => void;
  setSaveError: (error: string | null) => void;
}

export const useSettingsStore = create<SettingsState>()((set) => ({
  settings: null,
  isLoaded: false,
  isSaving: false,
  saveError: null,

  setSettings(settings) {
    set({ settings, isLoaded: true });
  },

  patchRuntime(patch) {
    set((s) =>
      s.settings
        ? { settings: { ...s.settings, runtime: { ...s.settings.runtime, ...patch } } }
        : {},
    );
  },

  patchPrivacy(patch) {
    set((s) =>
      s.settings
        ? { settings: { ...s.settings, privacy: { ...s.settings.privacy, ...patch } } }
        : {},
    );
  },

  patchNetwork(patch) {
    set((s) =>
      s.settings
        ? { settings: { ...s.settings, network: { ...s.settings.network, ...patch } } }
        : {},
    );
  },

  patchApp(patch) {
    set((s) =>
      s.settings
        ? { settings: { ...s.settings, app: { ...s.settings.app, ...patch } } }
        : {},
    );
  },

  setSaving(isSaving) {
    set({ isSaving });
  },

  setSaveError(saveError) {
    set({ saveError });
  },
}));
