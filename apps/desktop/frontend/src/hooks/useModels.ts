/**
 * useModels - facade over modelStore.
 *
 * Auto-fetches on mount. Re-fetches on window visibility change (tab/focus
 * restored) so stale installed/progress state is refreshed automatically.
 *
 * Exposes all actions needed by ModelLibrary, ModelCard, and the FirstRun wizard.
 */

import { useEffect } from 'react';
import { useModelStore } from '@/stores/modelStore';
import type { CatalogModel } from '@/stores/modelStore';

export type { CatalogModel };

export interface UseModelsReturn {
  models: CatalogModel[];
  activeModelSlug: string | null;
  installing: Record<string, boolean>;
  loading: Record<string, boolean>;
  loadError: Record<string, string>;
  isFetching: boolean;
  fetchError: string | null;
  fetch: () => Promise<void>;
  install: (slug: string) => Promise<void>;
  cancelInstall: (slug: string) => Promise<void>;
  remove: (slug: string) => Promise<void>;
  load: (slug: string) => Promise<void>;
  unload: (slug: string) => Promise<void>;
}

export function useModels(): UseModelsReturn {
  const store = useModelStore();

  // Fetch on mount
  useEffect(() => {
    void store.fetchModels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-fetch when user returns to the window
  useEffect(() => {
    function handleVisibilityChange() {
      if (document.visibilityState === 'visible') {
        void store.fetchModels();
      }
    }
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    models: store.models,
    activeModelSlug: store.activeModelSlug,
    installing: store.installing,
    loading: store.loading,
    loadError: store.loadError,
    isFetching: store.isFetching,
    fetchError: store.fetchError,
    fetch: store.fetchModels,
    install: store.install,
    cancelInstall: store.cancelInstall,
    remove: store.remove,
    load: store.load,
    unload: store.unload,
  };
}
