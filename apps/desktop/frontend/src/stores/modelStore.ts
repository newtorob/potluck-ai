/**
 * Model store — single source of truth for the model catalog.
 *
 * The catalog is the raw list from GET /models, augmented with live
 * download_progress polled from GET /models/{slug}/download-progress.
 *
 * Shape mirrors the Python sidecar response so we avoid a translation
 * layer and can pass objects straight to components.
 *
 * See guide §4.2.
 */

import { create } from 'zustand';
import { localApi } from '@/lib/localApi';

// ---------------------------------------------------------------------------
// API response shapes (snake_case to match sidecar JSON)
// ---------------------------------------------------------------------------

export type ModelTier = 'small' | 'medium' | 'large';

export interface DownloadProgressRecord {
  model_id: string;
  bytes_downloaded: number;
  bytes_total: number;
  speed_bps: number;
  eta_seconds: number;
  status: 'downloading' | 'verifying' | 'complete' | 'error' | 'cancelled';
  error?: string;
}

export interface CatalogModel {
  slug: string;
  name: string;
  tier: ModelTier;
  description: string;
  size_bytes: number;
  min_ram_gb: number;
  min_vram_gb: number;
  context_window: number;
  recommended_tier: ModelTier;
  is_featured: boolean;
  installed: boolean;
  /** Populated by the polling loop — not present in the GET /models response */
  download_progress?: DownloadProgressRecord;
}

// ---------------------------------------------------------------------------
// Store state and actions
// ---------------------------------------------------------------------------

interface ModelState {
  /** Full catalog from GET /models, merged with live download_progress */
  models: CatalogModel[];

  /** Slug of the currently-loaded model (from settings.active_model_id) */
  activeModelSlug: string | null;

  /** True while a POST /install request is pending (before polling starts) */
  installing: Record<string, boolean>;

  /** True while a POST /load or POST /unload request is in-flight */
  loading: Record<string, boolean>;

  /** Per-slug error from load/unload failures (cleared on next action) */
  loadError: Record<string, string>;

  /** True while the initial GET /models fetch is running */
  isFetching: boolean;

  /** Non-null if GET /models itself fails */
  fetchError: string | null;

  // ---- polling internals (not exposed to consumers) ----------------------
  _pollTimers: Record<string, ReturnType<typeof setInterval>>;

  // ---- actions ------------------------------------------------------------
  fetchModels: () => Promise<void>;
  install: (slug: string) => Promise<void>;
  cancelInstall: (slug: string) => Promise<void>;
  remove: (slug: string) => Promise<void>;
  load: (slug: string) => Promise<void>;
  unload: (slug: string) => Promise<void>;

  // Internal helpers
  _startPolling: (slug: string) => void;
  _stopPolling: (slug: string) => void;
  _mergeProgress: (slug: string, progress: DownloadProgressRecord) => void;
  _clearProgress: (slug: string) => void;
  _setInstalling: (slug: string, value: boolean) => void;
  _setLoading: (slug: string, value: boolean) => void;
  _setLoadError: (slug: string, error: string | null) => void;
}

// ---------------------------------------------------------------------------
// Store implementation
// ---------------------------------------------------------------------------

export const useModelStore = create<ModelState>()((set, get) => ({
  models: [],
  activeModelSlug: null,
  installing: {},
  loading: {},
  loadError: {},
  isFetching: false,
  fetchError: null,
  _pollTimers: {},

  // ---- fetchModels -------------------------------------------------------

  async fetchModels() {
    set({ isFetching: true, fetchError: null });
    try {
      // Fetch catalog and runtime state in parallel.
      // /health is authoritative for "what's loaded right now" — settings may lag
      // because the sidecar doesn't persist active_model_id across loads in v0.
      const [rawModels, health] = await Promise.all([
        (localApi.listModels() as unknown) as Promise<CatalogModel[]>,
        localApi.health().catch(() => null),
      ]);

      const activeModelSlug = health?.active_model_id ?? null;

      // Preserve any existing download_progress data for models still downloading
      const existingModels = get().models;
      const existingProgress: Record<string, DownloadProgressRecord | undefined> = {};
      for (const m of existingModels) {
        if (m.download_progress) {
          existingProgress[m.slug] = m.download_progress;
        }
      }

      const merged: CatalogModel[] = rawModels.map((m) => {
        const existing = existingProgress[m.slug];
        if (existing !== undefined) {
          return { ...m, download_progress: existing };
        }
        // Return without the key so exactOptionalPropertyTypes is satisfied
        const { download_progress: _removed, ...rest } = m;
        void _removed;
        return rest;
      });

      set({ models: merged, activeModelSlug, isFetching: false });
    } catch (err) {
      set({
        fetchError: err instanceof Error ? err.message : 'Failed to fetch models',
        isFetching: false,
      });
    }
  },

  // ---- install -----------------------------------------------------------

  async install(slug) {
    get()._setInstalling(slug, true);
    try {
      await localApi.installModel(slug);
      // Start polling download progress
      get()._startPolling(slug);
    } catch (err) {
      get()._setInstalling(slug, false);
      get()._setLoadError(slug, err instanceof Error ? err.message : 'Install failed');
    }
  },

  // ---- cancelInstall -----------------------------------------------------

  async cancelInstall(slug) {
    get()._stopPolling(slug);
    try {
      await fetch(`http://127.0.0.1:8321/models/${encodeURIComponent(slug)}/cancel-download`, {
        method: 'POST',
      });
    } catch {
      // Best-effort cancel — ignore network errors
    }
    get()._clearProgress(slug);
    get()._setInstalling(slug, false);
    // Refresh so installed flag is accurate
    void get().fetchModels();
  },

  // ---- remove ------------------------------------------------------------

  async remove(slug) {
    get()._stopPolling(slug);
    try {
      await localApi.deleteModel(slug);
      // If we removed the active model, clear the slug
      if (get().activeModelSlug === slug) {
        set({ activeModelSlug: null });
      }
    } catch (err) {
      get()._setLoadError(slug, err instanceof Error ? err.message : 'Delete failed');
    }
    void get().fetchModels();
  },

  // ---- load --------------------------------------------------------------

  async load(slug) {
    get()._setLoading(slug, true);
    get()._setLoadError(slug, null);
    try {
      await localApi.loadModel(slug);
      set({ activeModelSlug: slug });
      void get().fetchModels();
    } catch (err) {
      get()._setLoadError(slug, err instanceof Error ? err.message : 'Failed to load model');
    } finally {
      get()._setLoading(slug, false);
    }
  },

  // ---- unload ------------------------------------------------------------

  async unload(slug) {
    get()._setLoading(slug, true);
    get()._setLoadError(slug, null);
    try {
      await fetch(`http://127.0.0.1:8321/models/${encodeURIComponent(slug)}/unload`, {
        method: 'POST',
      });
      if (get().activeModelSlug === slug) {
        set({ activeModelSlug: null });
      }
      void get().fetchModels();
    } catch (err) {
      get()._setLoadError(slug, err instanceof Error ? err.message : 'Failed to unload model');
    } finally {
      get()._setLoading(slug, false);
    }
  },

  // ---- internal helpers --------------------------------------------------

  _startPolling(slug) {
    // Clear any pre-existing timer for this slug
    get()._stopPolling(slug);

    const timer = setInterval(() => {
      const store = get();

      fetch(`http://127.0.0.1:8321/models/${encodeURIComponent(slug)}/download-progress`)
        .then(async (res) => {
          if (res.status === 404) {
            // No record — download may not have started yet; keep polling briefly
            return;
          }
          if (!res.ok) return;
          const progress = (await res.json()) as DownloadProgressRecord;
          store._mergeProgress(slug, progress);

          if (
            progress.status === 'complete' ||
            progress.status === 'error' ||
            progress.status === 'cancelled'
          ) {
            store._stopPolling(slug);
            store._setInstalling(slug, false);
            if (progress.status === 'complete') {
              void store.fetchModels();
            }
          }
        })
        .catch(() => {
          // Transient network error — keep polling
        });
    }, 1000);

    set((s) => ({ _pollTimers: { ...s._pollTimers, [slug]: timer } }));
    // Mark as installing so UI shows progress immediately
    get()._setInstalling(slug, true);
  },

  _stopPolling(slug) {
    const timer = get()._pollTimers[slug];
    if (timer !== undefined) {
      clearInterval(timer);
      set((s) => {
        const next = { ...s._pollTimers };
        delete next[slug];
        return { _pollTimers: next };
      });
    }
  },

  _mergeProgress(slug, progress) {
    set((s) => ({
      models: s.models.map((m) =>
        m.slug === slug ? { ...m, download_progress: progress } : m,
      ),
    }));
  },

  _clearProgress(slug) {
    set((s) => ({
      models: s.models.map((m) => {
        if (m.slug !== slug) return m;
        // Remove the key entirely to satisfy exactOptionalPropertyTypes
        const { download_progress: _removed, ...rest } = m;
        void _removed;
        return rest;
      }),
    }));
  },

  _setInstalling(slug, value) {
    set((s) => ({ installing: { ...s.installing, [slug]: value } }));
  },

  _setLoading(slug, value) {
    set((s) => ({ loading: { ...s.loading, [slug]: value } }));
  },

  _setLoadError(slug, error) {
    set((s) => ({
      loadError: error === null
        ? (({ [slug]: _removed, ...rest }) => rest)(s.loadError)
        : { ...s.loadError, [slug]: error },
    }));
  },
}));
