/**
 * useSettings - loads and persists user settings via the Python sidecar.
 *
 * Also applies the `theme` setting to the <html> element so Tailwind's
 * darkMode: 'class' variant activates correctly.
 */

import { useEffect } from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import type { PotluckSettings, PotluckSettingsPatch } from '@/lib/localApi';

export function applyTheme(theme: 'system' | 'light' | 'dark') {
  const html = document.documentElement;
  if (theme === 'dark') {
    html.classList.add('dark');
  } else if (theme === 'light') {
    html.classList.remove('dark');
  } else {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    html.classList.toggle('dark', prefersDark);
  }
}

export interface UseSettingsReturn {
  settings: PotluckSettings | null;
  isLoaded: boolean;
  isLoading: boolean;
  loadError: string | null;
  isSaving: boolean;
  saveError: string | null;
  update: (patch: PotluckSettingsPatch) => Promise<void>;
  retry: () => Promise<void>;
}

export function useSettings(): UseSettingsReturn {
  const store = useSettingsStore();

  const retry = async () => {
    await store.fetch();
    const settings = useSettingsStore.getState().settings;
    if (settings) applyTheme(settings.app.theme);
  };

  // Fetch on mount, apply theme when settings arrive
  useEffect(() => {
    void retry();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Wrap update to also apply theme changes
  const update = async (patch: PotluckSettingsPatch) => {
    await store.update(patch);
    const s = useSettingsStore.getState().settings;
    if (s) applyTheme(s.app.theme);
  };

  return {
    settings: store.settings,
    isLoaded: store.isLoaded,
    isLoading: store.isLoading,
    loadError: store.loadError,
    isSaving: store.isSaving,
    saveError: store.saveError,
    update,
    retry,
  };
}
