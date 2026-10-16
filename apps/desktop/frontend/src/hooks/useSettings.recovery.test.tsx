import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/localApi', () => ({
  localApi: { getSettings: vi.fn(), updateSettings: vi.fn() },
}));

import { localApi, type PotluckSettings } from '@/lib/localApi';
import { useSettingsStore } from '@/stores/settingsStore';
import { useSettings } from './useSettings';

function settings(theme: 'light' | 'dark' = 'light'): PotluckSettings {
  return {
    runtime: { context_size: 4096, gpu_layers: 0, threads: 4, temperature: 0.7, max_tokens: 512 },
    privacy: {
      chat_history_enabled: false,
      telemetry_enabled: false,
      crash_reports_enabled: false,
      memory_enabled: false,
    },
    network: {
      contributor_mode: false,
      contributor_schedule: null,
      max_gpu_percent: 50,
      max_concurrent_jobs: 1,
      coordinator_url: '',
    },
    app: { theme, font_size: 14 },
    memory: {
      enabled: false,
      auto_extract: false,
      auto_retrieve: false,
      extraction_frequency: 1,
      max_memories: 100,
      max_retrieval: 5,
      min_confidence_for_retrieval: 0.5,
      retention_days: 0,
      categories_enabled: [],
    },
    vault: { folder: null, last_scan_at: null, file_count: 0, chunk_count: 0 },
    gateway: { enabled: false, api_key: '', pool_routing: false },
    models_path: '',
    active_model_id: null,
    active_project: null,
    ui: {},
  };
}

const unfinished: Array<() => void> = [];
function response() {
  let resolve!: (value: PotluckSettings) => void;
  let reject!: (error: Error) => void;
  let settled = false;
  const promise = new Promise<PotluckSettings>((yes, no) => {
    resolve = (value) => {
      settled = true;
      yes(value);
    };
    reject = (error) => {
      settled = true;
      no(error);
    };
  });
  unfinished.push(() => {
    if (!settled) resolve(settings());
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  useSettingsStore.setState(useSettingsStore.getInitialState(), true);
  document.documentElement.classList.remove('dark');
});

afterEach(async () => {
  // Also release deferred reads if an assertion fails, avoiding cross-test state.
  await act(async () => {
    unfinished.splice(0).forEach((finish) => finish());
  });
  vi.restoreAllMocks();
});

describe('mounted settings recovery', () => {
  it('stops loading after the first GET fails, without automatically retrying forever', async () => {
    const read = response();
    vi.mocked(localApi.getSettings).mockReturnValue(read.promise);
    const { result } = renderHook(() => useSettings());
    await act(async () => {
      read.reject(new Error('offline'));
    });

    expect(result.current.settings).toBeNull();
    expect(result.current.isLoading).toBe(false);
    expect(result.current.loadError).toEqual(expect.any(String));
    expect(result.current.loadError).not.toBe('');
    expect(localApi.getSettings).toHaveBeenCalledTimes(1);
  });

  it('retries in the same mounted hook, retaining the error until settings and theme recover', async () => {
    vi.mocked(localApi.getSettings).mockRejectedValueOnce(new Error('offline'));
    const { result } = renderHook(() => useSettings());
    await act(async () => {});
    const originalError = result.current.loadError;
    const retry = response();
    vi.mocked(localApi.getSettings).mockReturnValueOnce(retry.promise);
    let done!: Promise<void>;
    act(() => {
      done = result.current.retry();
    });
    expect(result.current.isLoading).toBe(true);
    expect(result.current.loadError).toBe(originalError);
    expect(result.current.settings).toBeNull();

    const recovered = settings('dark');
    await act(async () => {
      retry.resolve(recovered);
      await done;
    });
    expect(result.current.settings).toEqual(recovered);
    expect(result.current.isLoaded).toBe(true);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.loadError).toBeNull();
    expect(document.documentElement).toHaveClass('dark');
    expect(localApi.getSettings).toHaveBeenCalledTimes(2);
  });

  it('keeps cached settings usable when a refresh fails, and retry replaces them without remounting', async () => {
    const cached = settings('dark');
    useSettingsStore.getState().hydrateFromSidecar(cached);
    vi.mocked(localApi.getSettings).mockRejectedValueOnce(new Error('sidecar restarting'));
    const { result } = renderHook(() => useSettings());
    await act(async () => {});

    expect(result.current.settings).toEqual(cached);
    expect(result.current.isLoaded).toBe(true);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.loadError).toEqual(expect.any(String));

    const current = settings('light');
    vi.mocked(localApi.getSettings).mockResolvedValueOnce(current);
    await act(async () => {
      await result.current.retry();
    });
    expect(result.current.settings).toEqual(current);
    expect(result.current.loadError).toBeNull();
    expect(document.documentElement).not.toHaveClass('dark');
  });

  it('shares one pending read across mounted consumers and concurrent retries', async () => {
    const initial = response();
    vi.mocked(localApi.getSettings).mockReturnValueOnce(initial.promise);
    const { result } = renderHook(() => [useSettings(), useSettings()] as const);
    await act(async () => {
      initial.resolve(settings());
    });
    expect(localApi.getSettings).toHaveBeenCalledTimes(1);
    expect(result.current[0].settings).toEqual(result.current[1].settings);

    const retry = response();
    vi.mocked(localApi.getSettings).mockReturnValueOnce(retry.promise);
    let done!: Promise<void[]>;
    act(() => {
      done = Promise.all([result.current[0].retry(), result.current[1].retry()]);
    });
    await act(async () => {
      retry.resolve(settings('dark'));
      await done;
    });
    expect(localApi.getSettings).toHaveBeenCalledTimes(2);
    expect(result.current[0].settings?.app.theme).toBe('dark');
    expect(result.current[1].settings?.app.theme).toBe('dark');
  });
});

describe('settings recovery preserves newer state', () => {
  it.each(['success', 'failure'] as const)(
    'an obsolete GET %s cannot finish or replace a newer mounted retry',
    async (outcome) => {
      const initial = response();
      vi.mocked(localApi.getSettings).mockReturnValueOnce(initial.promise);
      const { result } = renderHook(() => useSettings());
      const hydrated = settings('dark');
      act(() => {
        useSettingsStore.getState().hydrateFromSidecar(hydrated);
      });
      const retry = response();
      vi.mocked(localApi.getSettings).mockReturnValueOnce(retry.promise);
      let done!: Promise<void>;
      act(() => {
        done = result.current.retry();
      });

      await act(async () => {
        if (outcome === 'success') initial.resolve(settings('light'));
        else initial.reject(new Error('obsolete offline response'));
      });
      expect(result.current.isLoading).toBe(true);
      expect(result.current.loadError).toBeNull();
      expect(result.current.settings).toEqual(hydrated);
      expect(localApi.getSettings).toHaveBeenCalledTimes(2);

      const current = { ...settings('light'), active_project: 'current project' };
      await act(async () => {
        retry.resolve(current);
        await done;
      });
      expect(result.current.isLoading).toBe(false);
      expect(result.current.settings).toEqual(current);
      expect(result.current.loadError).toBeNull();
    },
  );

  it.each(['success', 'failure'] as const)(
    'ignores an older GET %s after startup hydration has supplied settings',
    async (outcome) => {
      const read = response();
      vi.mocked(localApi.getSettings).mockReturnValueOnce(read.promise);
      const done = useSettingsStore.getState().fetch();
      const hydrated = settings('dark');
      useSettingsStore.getState().hydrateFromSidecar(hydrated);
      if (outcome === 'success') read.resolve(settings('light'));
      else read.reject(new Error('old request failed'));
      await done;

      expect(useSettingsStore.getState().settings).toEqual(hydrated);
      expect(useSettingsStore.getState().isLoading).toBe(false);
      expect(useSettingsStore.getState().loadError).toBeNull();
    },
  );

  it('does not allow delayed startup hydration to replace a successful read', async () => {
    const fetched = settings('dark');
    vi.mocked(localApi.getSettings).mockResolvedValueOnce(fetched);
    await useSettingsStore.getState().fetch();
    useSettingsStore.getState().hydrateFromSidecar(settings('light'));
    expect(useSettingsStore.getState().settings).toEqual(fetched);
  });

  it.each(['success', 'failure'] as const)(
    'an old GET %s cannot replace an optimistic edit or its saved response',
    async (outcome) => {
      useSettingsStore.getState().hydrateFromSidecar(settings('light'));
      const read = response();
      const write = response();
      vi.mocked(localApi.getSettings).mockReturnValueOnce(read.promise);
      vi.mocked(localApi.updateSettings).mockReturnValueOnce(write.promise);
      const readDone = useSettingsStore.getState().fetch();
      const writeDone = useSettingsStore.getState().update({ app: { theme: 'dark' } });
      if (outcome === 'success') read.resolve(settings('light'));
      else read.reject(new Error('old offline result'));
      await readDone;

      const pending = useSettingsStore.getState();
      expect(pending.settings?.app.theme).toBe('dark');
      expect(pending.isSaving).toBe(true);
      expect(pending.loadError).toBeNull();

      write.resolve(settings('dark'));
      await writeDone;
      expect(useSettingsStore.getState().settings?.app.theme).toBe('dark');
      expect(useSettingsStore.getState().isSaving).toBe(false);
    },
  );

  it.each(['saved', 'rolled back'] as const)(
    'a refresh started during a write cannot replace its %s result',
    async (outcome) => {
      useSettingsStore.getState().hydrateFromSidecar(settings('light'));
      const read = response();
      const write = response();
      vi.mocked(localApi.getSettings).mockReturnValue(read.promise);
      vi.mocked(localApi.updateSettings).mockReturnValueOnce(write.promise);
      const writeDone = useSettingsStore
        .getState()
        .update({ app: { theme: 'dark' } })
        .catch(() => undefined);
      const readDone = useSettingsStore.getState().fetch();
      if (outcome === 'saved') write.resolve(settings('dark'));
      else write.reject(new Error('write refused'));
      await writeDone;
      const settled = useSettingsStore.getState().settings;
      // This read began before the write settled: it is not evidence of current settings.
      read.resolve(settings(outcome === 'saved' ? 'light' : 'dark'));
      await readDone;

      expect(useSettingsStore.getState().settings).toEqual(settled);
      expect(useSettingsStore.getState().loadError).toBeNull();
      expect(useSettingsStore.getState().isSaving).toBe(false);
      if (outcome === 'rolled back')
        expect(useSettingsStore.getState().saveError).toBe('write refused');
    },
  );

  it('keeps save and load failures separate and recovers a later read after a failed edit', async () => {
    useSettingsStore.getState().hydrateFromSidecar(settings('light'));
    vi.mocked(localApi.updateSettings).mockRejectedValueOnce(new Error('save refused'));
    await expect(useSettingsStore.getState().update({ app: { theme: 'dark' } })).rejects.toThrow(
      'save refused',
    );
    vi.mocked(localApi.getSettings).mockRejectedValueOnce(new Error('offline'));
    await useSettingsStore.getState().fetch();
    expect(useSettingsStore.getState().settings?.app.theme).toBe('light');
    expect(useSettingsStore.getState().saveError).toBe('save refused');
    expect(useSettingsStore.getState().loadError).toEqual(expect.any(String));

    vi.mocked(localApi.getSettings).mockResolvedValueOnce(settings('dark'));
    await useSettingsStore.getState().fetch();
    await waitFor(() => expect(useSettingsStore.getState().isLoading).toBe(false));
    expect(useSettingsStore.getState().settings?.app.theme).toBe('dark');
    expect(useSettingsStore.getState().loadError).toBeNull();
  });
});
