/**
 * useSettings — loads and persists user settings via the Python sidecar.
 *
 * Also applies the `theme` setting to the <html> element so Tailwind's
 * darkMode: 'class' variant activates correctly.
 */

import { useEffect, useCallback } from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import { localApi } from '@/lib/localApi';
import type { PotluckSettings } from '@/lib/localApi';

interface UseSettingsReturn {
  settings: PotluckSettings | null;
  isLoaded: boolean;
  isSaving: boolean;
  saveError: string | null;
  update: (patch: Partial<PotluckSettings>) => Promise<void>;
}

function applyTheme(theme: 'system' | 'light' | 'dark') {
  const html = document.documentElement;
  if (theme === 'dark') {
    html.classList.add('dark');
  } else if (theme === 'light') {
    html.classList.remove('dark');
  } else {
    // System: follow prefers-color-scheme
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    html.classList.toggle('dark', prefersDark);
  }
}

export function useSettings(): UseSettingsReturn {
  const store = useSettingsStore();

  // Load settings from sidecar on mount
  useEffect(() => {
    let cancelled = false;

    localApi
      .getSettings()
      .then((settings) => {
        if (cancelled) return;
        store.setSettings(settings);
        applyTheme(settings.app.theme);
      })
      .catch((err) => {
        if (cancelled) return;
        // Sidecar may not be running during pure UI dev — log and continue
        console.warn('[useSettings] Could not load settings from sidecar:', err);
      });

    return () => {
      cancelled = true;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const update = useCallback(
    async (patch: Partial<PotluckSettings>) => {
      store.setSaving(true);
      store.setSaveError(null);
      try {
        const updated = await localApi.updateSettings(patch);
        store.setSettings(updated);
        if (patch.app?.theme) {
          applyTheme(patch.app.theme);
        }
      } catch (err) {
        store.setSaveError(err instanceof Error ? err.message : 'Failed to save settings');
        throw err;
      } finally {
        store.setSaving(false);
      }
    },
    [store],
  );

  return {
    settings: store.settings,
    isLoaded: store.isLoaded,
    isSaving: store.isSaving,
    saveError: store.saveError,
    update,
  };
}
