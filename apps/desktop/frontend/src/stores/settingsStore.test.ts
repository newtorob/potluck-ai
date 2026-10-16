/**
 * settingsStore - hydrateFromSidecar seeds the store from the single
 * GET /settings that durablePrefs already makes at startup, so privacy
 * gates that read settings have a real value before the first send.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('@/lib/localApi', () => ({
  localApi: {
    getSettings: vi.fn(),
    updateSettings: vi.fn(),
  },
}));

import { useSettingsStore } from './settingsStore';
import { localApi, type PotluckSettings } from '@/lib/localApi';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.resetAllMocks();
  useSettingsStore.setState({
    settings: null,
    isLoaded: false,
    isLoading: false,
    loadError: null,
    isSaving: false,
    saveError: null,
  });
});

function makeSettings(chatHistoryEnabled: boolean): PotluckSettings {
  return {
    runtime: { context_size: 4096, gpu_layers: 0, threads: 4, temperature: 0.7, max_tokens: 512 },
    privacy: {
      chat_history_enabled: chatHistoryEnabled,
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
    app: { theme: 'system', font_size: 14 },
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

describe('settingsStore.hydrateFromSidecar', () => {
  it('seeds settings without a second round-trip', () => {
    const settings = makeSettings(true);

    useSettingsStore.getState().hydrateFromSidecar(settings);

    const state = useSettingsStore.getState();
    expect(state.settings).toBe(settings);
    expect(state.settings?.privacy.chat_history_enabled).toBe(true);
    expect(state.isLoaded).toBe(true);
  });

  it('carries a disabled chat-history flag through', () => {
    useSettingsStore.getState().hydrateFromSidecar(makeSettings(false));

    expect(useSettingsStore.getState().settings?.privacy.chat_history_enabled).toBe(false);
  });

  it('does not overwrite a fetched value or a user update with a late startup response', async () => {
    const fetched = makeSettings(false);
    vi.mocked(localApi.getSettings).mockResolvedValue(fetched);
    await useSettingsStore.getState().fetch();
    useSettingsStore.getState().hydrateFromSidecar(makeSettings(true));
    expect(useSettingsStore.getState().settings).toBe(fetched);

    const save = deferred<PotluckSettings>();
    vi.mocked(localApi.updateSettings).mockReturnValue(save.promise);
    const update = useSettingsStore.getState().update({ app: { theme: 'dark' } });
    const optimistic = useSettingsStore.getState().settings;
    useSettingsStore.getState().hydrateFromSidecar(makeSettings(true));
    expect(useSettingsStore.getState().settings).toBe(optimistic);
    const saved = { ...fetched, app: { ...fetched.app, theme: 'dark' as const } };
    save.resolve(saved);
    await update;
    useSettingsStore.getState().hydrateFromSidecar(makeSettings(true));
    expect(useSettingsStore.getState().settings).toBe(saved);
  });
});

describe('settingsStore.fetch', () => {
  it('shares the active request and releases it when loading completes', async () => {
    const response = deferred<PotluckSettings>();
    vi.mocked(localApi.getSettings).mockReturnValue(response.promise);
    const first = useSettingsStore.getState().fetch();
    const second = useSettingsStore.getState().fetch();
    expect(second).toBe(first);
    expect(localApi.getSettings).toHaveBeenCalledOnce();
    expect(useSettingsStore.getState().isLoading).toBe(true);
    response.resolve(makeSettings(false));
    await first;
    expect(useSettingsStore.getState().isLoading).toBe(false);
    expect(useSettingsStore.getState().isLoaded).toBe(true);
  });

  it('can retry a synchronous API failure without reflecting its private message', async () => {
    vi.mocked(localApi.getSettings).mockImplementationOnce(() => {
      throw new Error('private-api-key');
    });
    await useSettingsStore.getState().fetch();
    expect(useSettingsStore.getState()).toMatchObject({
      settings: null,
      isLoading: false,
      isLoaded: false,
    });
    expect(useSettingsStore.getState().loadError).toContain('Could not load settings');
    expect(useSettingsStore.getState().loadError).not.toContain('private-api-key');

    const response = deferred<PotluckSettings>();
    vi.mocked(localApi.getSettings).mockReturnValue(response.promise);
    const retry = useSettingsStore.getState().fetch();
    expect(useSettingsStore.getState().isLoading).toBe(true);
    expect(useSettingsStore.getState().loadError).toContain('Could not load settings');
    response.resolve(makeSettings(false));
    await retry;
    expect(localApi.getSettings).toHaveBeenCalledTimes(2);
    expect(useSettingsStore.getState()).toMatchObject({
      isLoading: false,
      isLoaded: true,
      loadError: null,
    });
  });

  it('preserves cached settings and save feedback when refreshing fails', async () => {
    const cached = makeSettings(false);
    useSettingsStore.getState().hydrateFromSidecar(cached);
    useSettingsStore.setState({ saveError: 'A previous save failed' });
    vi.mocked(localApi.getSettings).mockRejectedValue(new Error('offline'));
    await useSettingsStore.getState().fetch();
    expect(useSettingsStore.getState()).toMatchObject({
      settings: cached,
      isLoaded: true,
      isLoading: false,
      saveError: 'A previous save failed',
    });
    expect(useSettingsStore.getState().loadError).toContain('Could not load settings');
  });

  it('does not let an obsolete completion clear a newer request loading state', async () => {
    const obsolete = deferred<PotluckSettings>();
    const current = deferred<PotluckSettings>();
    vi.mocked(localApi.getSettings)
      .mockReturnValueOnce(obsolete.promise)
      .mockReturnValueOnce(current.promise);
    const first = useSettingsStore.getState().fetch();
    const hydrated = makeSettings(false);
    useSettingsStore.getState().hydrateFromSidecar(hydrated);
    const second = useSettingsStore.getState().fetch();
    obsolete.reject(new Error('old request failed'));
    await first;
    expect(useSettingsStore.getState()).toMatchObject({
      settings: hydrated,
      isLoading: true,
      loadError: null,
    });
    const fresh = makeSettings(true);
    current.resolve(fresh);
    await second;
    expect(useSettingsStore.getState()).toMatchObject({
      settings: fresh,
      isLoading: false,
      loadError: null,
    });
  });
});
