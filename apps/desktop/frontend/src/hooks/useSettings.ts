/**
 * useSettings — loads and persists user settings via the Python sidecar.
 *
 * Also applies the `theme` setting to the <html> element so Tailwind's
 * darkMode: 'class' variant activates correctly.
 */

import { useEffect } from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import type { PotluckSettings } from '@/lib/localApi';

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
  isSaving: boolean;
  saveError: string | null;
  update: (patch: Partial<PotluckSettings>) => Promise<void>;
}

export function useSettings(): UseSettingsReturn {
  const store = useSettingsStore();

  // Fetch on mount, apply theme when settings arrive
  useEffect(() => {
    void store.fetch().then(() => {
      const s = useSettingsStore.getState().settings;
      if (s) applyTheme(s.app.theme);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Wrap update to also apply theme changes
  const update = async (patch: Partial<PotluckSettings>) => {
    await store.update(patch);
    const s = useSettingsStore.getState().settings;
    if (s) applyTheme(s.app.theme);
  };

  return {
    settings: store.settings,
    isLoaded: store.isLoaded,
    isSaving: store.isSaving,
    saveError: store.saveError,
    update,
  };
}
