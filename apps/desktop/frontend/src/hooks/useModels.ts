/**
 * useModels — loads the model list from the local sidecar and exposes
 * install / delete / load actions bound to the model store.
 */

import { useEffect, useCallback } from 'react';
import { useModelStore } from '@/stores/modelStore';
import { localApi } from '@/lib/localApi';
import type { InstalledModel } from '@/types/models';

interface UseModelsReturn {
  availableModels: ReturnType<typeof useModelStore.getState>['availableModels'];
  installedModels: InstalledModel[];
  activeModelId: string | null;
  isLoading: boolean;
  loadError: string | null;
  install: (modelId: string) => Promise<void>;
  remove: (modelId: string) => Promise<void>;
  activate: (modelId: string) => Promise<void>;
  refresh: () => Promise<void>;
}

export function useModels(): UseModelsReturn {
  const store = useModelStore();

  const refresh = useCallback(async () => {
    store.setLoading(true);
    store.setLoadError(null);
    try {
      const models = await localApi.listModels();
      const installed = models.filter((m): m is InstalledModel => m.installed);
      store.setModels(models, installed);
    } catch (err) {
      store.setLoadError(err instanceof Error ? err.message : 'Failed to load models');
    } finally {
      store.setLoading(false);
    }
  }, [store]);

  // Load on mount
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const install = useCallback(
    async (modelId: string) => {
      await localApi.installModel(modelId);
      void refresh();
    },
    [refresh],
  );

  const remove = useCallback(
    async (modelId: string) => {
      await localApi.deleteModel(modelId);
      if (store.activeModelId === modelId) {
        store.setActiveModel(null);
      }
      void refresh();
    },
    [store, refresh],
  );

  const activate = useCallback(
    async (modelId: string) => {
      await localApi.loadModel(modelId);
      store.setActiveModel(modelId);
    },
    [store],
  );

  return {
    availableModels: store.availableModels,
    installedModels: store.installedModels,
    activeModelId: store.activeModelId,
    isLoading: store.isLoading,
    loadError: store.loadError,
    install,
    remove,
    activate,
    refresh,
  };
}
