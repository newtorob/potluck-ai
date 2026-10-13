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
import type { PotluckSettings } from '@/lib/localApi';

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
  beforeEach(() => {
    useSettingsStore.setState({
      settings: null,
      isLoaded: false,
      isSaving: false,
      saveError: null,
    });
  });

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
});
