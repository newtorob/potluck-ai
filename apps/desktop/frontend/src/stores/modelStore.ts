/**
 * Model store — tracks available models, installed models, active model,
 * and in-progress downloads.
 *
 * Populated by useModels hook via localApi.listModels() on mount.
 */

import { create } from 'zustand';
import type { Model, InstalledModel, DownloadProgress } from '@/types/models';

interface ModelState {
  availableModels: Model[];
  installedModels: InstalledModel[];
  activeModelId: string | null;
  isLoading: boolean;
  loadError: string | null;
  downloads: Map<string, DownloadProgress>;

  // Actions
  setModels: (available: Model[], installed: InstalledModel[]) => void;
  setActiveModel: (id: string | null) => void;
  setLoading: (loading: boolean) => void;
  setLoadError: (error: string | null) => void;
  updateDownload: (progress: DownloadProgress) => void;
  clearDownload: (modelId: string) => void;
}

export const useModelStore = create<ModelState>()((set) => ({
  availableModels: [],
  installedModels: [],
  activeModelId: null,
  isLoading: false,
  loadError: null,
  downloads: new Map(),

  setModels(available, installed) {
    set({ availableModels: available, installedModels: installed });
  },

  setActiveModel(id) {
    set({ activeModelId: id });
  },

  setLoading(isLoading) {
    set({ isLoading });
  },

  setLoadError(loadError) {
    set({ loadError });
  },

  updateDownload(progress) {
    set((s) => {
      const next = new Map(s.downloads);
      next.set(progress.modelId, progress);
      return { downloads: next };
    });
  },

  clearDownload(modelId) {
    set((s) => {
      const next = new Map(s.downloads);
      next.delete(modelId);
      return { downloads: next };
    });
  },
}));
