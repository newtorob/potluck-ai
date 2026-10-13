/**
 * Download-complete auto-advance must call load() once. A whole-store
 * subscription in the effect deps would re-fire load on every zustand write
 * and hit the React update-depth limit during onboarding.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const loadModel = vi.fn().mockResolvedValue({ status: 'ok' });
const installModel = vi.fn().mockResolvedValue({ status: 'started' });

vi.mock('@/lib/localApi', () => ({
  localApi: {
    loadModel: (...args: unknown[]) => loadModel(...args),
    installModel: (...args: unknown[]) => installModel(...args),
    health: vi.fn().mockResolvedValue({
      model_loaded: false,
      active_model_id: null,
    }),
    listModels: vi.fn().mockResolvedValue([]),
    detectHardware: vi.fn().mockResolvedValue({
      os: 'macOS',
      arch: 'arm64',
      cpu_name: 'Apple M-series',
      cpu_cores_physical: 8,
      cpu_cores_logical: 8,
      ram_total_gb: 16,
      ram_available_gb: 10,
      gpu_name: 'Apple Silicon',
      gpu_vram_gb: null,
      recommended_tier: 'small',
    }),
  },
}));

vi.mock('@/hooks/useHardware', () => ({
  useHardware: () => ({
    hardware: {
      os: 'macOS',
      arch: 'arm64',
      cpu_name: 'Apple M-series',
      cpu_cores_physical: 8,
      cpu_cores_logical: 8,
      ram_total_gb: 16,
      ram_available_gb: 10,
      gpu_name: 'Apple Silicon',
      gpu_vram_gb: null,
      recommended_tier: 'small' as const,
    },
    isLoading: false,
    error: null,
    refresh: () => {},
  }),
}));

vi.mock('@/hooks/useHealth', () => ({
  useHealth: () => ({
    sidecarUp: true,
    modelLoaded: false,
    activeModelId: null,
    nCtxLoaded: null,
    loadedRpcServers: null,
    loadedPipeline: null,
    continuousBatch: false,
  }),
}));

vi.mock('@/stores/meshStore', () => ({
  useMeshStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      status: null,
      peers: [],
      bridge: 'sidecar_offline',
      start: () => {},
    }),
}));

import { FirstRun } from './FirstRun';
import { useModelStore, type CatalogModel } from '@/stores/modelStore';

const SMALL_MODEL: CatalogModel = {
  slug: 'tiny-test',
  name: 'Tiny Test',
  tier: 'small',
  description: 'Test model',
  size_bytes: 1_000_000,
  min_ram_gb: 4,
  min_vram_gb: 0,
  context_window: 4096,
  recommended_tier: 'small',
  is_featured: true,
  installed: false,
};

function advanceToPick() {
  fireEvent.click(screen.getByRole('button', { name: /get started/i }));
  fireEvent.click(screen.getByRole('button', { name: /continue/i }));
  expect(screen.getByText(/choose your first model/i)).toBeInTheDocument();
}

describe('FirstRun: download complete auto-advance', () => {
  beforeEach(() => {
    loadModel.mockReset().mockResolvedValue({ status: 'ok' });
    installModel.mockReset().mockResolvedValue({ status: 'started' });
    useModelStore.setState({
      models: [],
      activeModelSlug: null,
      installing: {},
      loading: {},
      loadError: {},
      isFetching: false,
      fetchError: null,
      _pollTimers: {},
    });
  });

  it('calls load() only once when download completes, even as the store keeps updating', async () => {
    let resolveLoad: ((value: { status: string }) => void) | undefined;
    loadModel.mockImplementation(
      () =>
        new Promise<{ status: string }>((resolve) => {
          resolveLoad = resolve;
        }),
    );

    const loadSpy = vi.spyOn(useModelStore.getState(), 'load');

    const { rerender } = render(
      <MemoryRouter>
        <FirstRun models={[SMALL_MODEL]} />
      </MemoryRouter>,
    );

    advanceToPick();

    // Simulate the polling loop flipping progress to complete, then writing
    // more store updates the way _setInstalling / fetchModels would.
    act(() => {
      useModelStore.getState()._mergeProgress('tiny-test', {
        model_id: 'tiny-test',
        bytes_downloaded: 1_000_000,
        bytes_total: 1_000_000,
        speed_bps: 0,
        eta_seconds: 0,
        status: 'complete',
      });
    });

    const modelsWithProgress: CatalogModel[] = [
      {
        ...SMALL_MODEL,
        installed: true,
        download_progress: {
          model_id: 'tiny-test',
          bytes_downloaded: 1_000_000,
          bytes_total: 1_000_000,
          speed_bps: 0,
          eta_seconds: 0,
          status: 'complete',
        },
      },
    ];

    rerender(
      <MemoryRouter>
        <FirstRun models={modelsWithProgress} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(loadSpy).toHaveBeenCalledTimes(1);
      expect(loadSpy).toHaveBeenCalledWith('tiny-test');
    });

    expect(screen.getByText(/loading model into memory/i)).toBeInTheDocument();

    // Extra store churn must not re-trigger the effect
    act(() => {
      useModelStore.getState()._setInstalling('tiny-test', false);
      useModelStore.getState()._setLoading('tiny-test', true);
      useModelStore.getState()._mergeProgress('tiny-test', {
        model_id: 'tiny-test',
        bytes_downloaded: 1_000_000,
        bytes_total: 1_000_000,
        speed_bps: 0,
        eta_seconds: 0,
        status: 'complete',
      });
    });

    rerender(
      <MemoryRouter>
        <FirstRun models={[...modelsWithProgress]} />
      </MemoryRouter>,
    );

    // Still loading - and still a single load() call
    expect(screen.getByText(/loading model into memory/i)).toBeInTheDocument();
    expect(loadSpy).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveLoad?.({ status: 'ok' });
    });

    loadSpy.mockRestore();
  });
});
