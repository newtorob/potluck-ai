/**
 * Download-complete auto-advance must call load() once. A whole-store
 * subscription in the effect deps would re-fire load on every zustand write
 * and hit the React update-depth limit during onboarding.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const modelSetupOptions = vi.fn();
const loadModel = vi.fn().mockResolvedValue({ status: 'ok' });
const installModel = vi.fn().mockResolvedValue({ status: 'started' });

vi.mock('@/lib/localApi', () => ({
  localApi: {
    modelSetupOptions: (...args: unknown[]) => modelSetupOptions(...args),
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

const CODER: CatalogModel = {
  ...SMALL_MODEL,
  slug: 'coder',
  name: 'Code Model',
  tier: 'medium',
  size_bytes: 4 * 1024 ** 3,
};
function options(use = 'general', models = [CODER, SMALL_MODEL]) {
  return {
    schemaVersion: 1,
    useCase: use,
    recommended: models.map((m) => ({ id: m.slug, basis: 'estimate', tokensPerSecond: null })),
    models: models.map((m) => ({
      id: m.slug,
      name: m.name,
      sizeBytes: m.size_bytes,
      fits: true,
      installed: m.installed,
      checked: null,
      useCases: ['coding'],
      recommendationReason: 'Within the memory budget.',
    })),
  };
}
function mount(models = [SMALL_MODEL, CODER]) {
  const rendered = render(
    <MemoryRouter>
      <FirstRun models={models} />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole('button', { name: /get started/i }));
  fireEvent.click(screen.getByRole('button', { name: /continue/i }));
  return {
    ...rendered,
    update: (next: CatalogModel[]) =>
      rendered.rerender(
        <MemoryRouter>
          <FirstRun models={next} />
        </MemoryRouter>,
      ),
  };
}
const downloadButton = () => screen.getByRole('button', { name: /download and load/i });
const coderRadio = () => screen.getByRole('radio', { name: /Code Model/ });
const complete = (model: CatalogModel): CatalogModel => ({
  ...model,
  installed: true,
  download_progress: {
    model_id: model.slug,
    bytes_downloaded: model.size_bytes,
    bytes_total: model.size_bytes,
    speed_bps: 0,
    eta_seconds: 0,
    status: 'complete',
  },
});

describe('FirstRun recommendations and consent', () => {
  beforeEach(() => {
    modelSetupOptions
      .mockReset()
      .mockImplementation((use: string) => Promise.resolve(options(use)));
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
    vi.spyOn(useModelStore.getState(), '_startPolling').mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it('uses engine order instead of hardware tier and preserves manual choices on catalog refresh', async () => {
    const view = mount();
    await waitFor(() => expect(coderRadio()).toBeChecked());
    expect(screen.getAllByText(/Estimated fit/)).toHaveLength(2);
    expect(loadModel).not.toHaveBeenCalled();
    expect(installModel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('radio', { name: /Tiny Test/ }));
    view.update([{ ...CODER }, { ...SMALL_MODEL }]);
    expect(screen.getByRole('radio', { name: /Tiny Test/ })).toBeChecked();
    fireEvent.keyDown(window, { key: 'Enter' });
    expect(screen.getByText('Choose your first model')).toBeInTheDocument();
  });

  it('ignores an older use response and prevents download while the new choice is pending', async () => {
    let resolveOld!: (value: unknown) => void;
    modelSetupOptions.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    mount();
    expect(downloadButton()).toBeDisabled();
    fireEvent.click(screen.getByRole('radio', { name: 'Coding' }));
    await waitFor(() => expect(coderRadio()).toBeChecked());
    expect(modelSetupOptions).toHaveBeenLastCalledWith('coding', expect.any(AbortSignal));
    await act(async () => resolveOld(options('general', [SMALL_MODEL])));
    expect(coderRadio()).toBeChecked();
    expect(screen.getByRole('radio', { name: 'Coding' })).toBeChecked();
  });

  it('does not revive old choices when returning to a purpose while its refresh is pending', async () => {
    mount();
    await waitFor(() => expect(coderRadio()).toBeChecked());
    modelSetupOptions.mockImplementation(() => new Promise(() => {}));
    fireEvent.click(screen.getByRole('radio', { name: 'Coding' }));
    expect(downloadButton()).toBeDisabled();
    fireEvent.click(screen.getByRole('radio', { name: 'General' }));
    expect(downloadButton()).toBeDisabled();
    expect(screen.queryByRole('radio', { name: /Code Model/ })).not.toBeInTheDocument();
  });

  it('does not fall back to the catalog when no model is eligible', async () => {
    modelSetupOptions.mockResolvedValue(options('general', []));
    mount();
    expect(await screen.findByText('No suitable model right now')).toBeInTheDocument();
    expect(downloadButton()).toBeDisabled();
    expect(screen.queryByRole('radio', { name: /Tiny Test/ })).not.toBeInTheDocument();
  });

  it('recovers from engine failure and refuses an old recommendation contract', async () => {
    modelSetupOptions
      .mockRejectedValueOnce(new Error('Engine unavailable'))
      .mockResolvedValueOnce({ schemaVersion: 1, recommendedModel: 'tiny-test', models: [] });
    mount();
    expect(await screen.findByText('Engine unavailable')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    expect(await screen.findByText(/Update the Potluck engine/)).toBeInTheDocument();
    expect(downloadButton()).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    await waitFor(() => expect(coderRadio()).toBeChecked());
    expect(downloadButton()).toBeEnabled();
  });

  it('requires an explicit load for an old completed download', async () => {
    mount([complete(CODER)]);
    await waitFor(() => expect(coderRadio()).toBeChecked());
    expect(loadModel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /Load model/ }));
    await waitFor(() => expect(loadModel).toHaveBeenCalledExactlyOnceWith('coder'));
  });

  it('loads a requested download once and freezes choices during download', async () => {
    let resolveLoad!: (value: unknown) => void;
    loadModel.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveLoad = resolve;
        }),
    );
    const view = mount();
    await waitFor(() => expect(coderRadio()).toBeChecked());
    fireEvent.click(downloadButton());
    await waitFor(() => expect(installModel).toHaveBeenCalledExactlyOnceWith('coder'));
    expect(screen.getByRole('radio', { name: 'Writing' })).toBeDisabled();
    expect(screen.getByRole('radio', { name: /Tiny Test/ })).toBeDisabled();
    view.update([complete(CODER), SMALL_MODEL]);
    await waitFor(() => expect(loadModel).toHaveBeenCalledExactlyOnceWith('coder'));
    act(() => useModelStore.getState()._setInstalling('coder', false));
    view.update([complete(CODER), { ...SMALL_MODEL }]);
    expect(screen.getByText('Loading model into memory')).toBeInTheDocument();
    expect(loadModel).toHaveBeenCalledTimes(1);
    await act(async () => resolveLoad({ status: 'ok' }));
    expect(screen.queryByText('Loading model into memory')).not.toBeInTheDocument();
  });

  it('keeps failed loads on the error step and supports an explicit retry', async () => {
    loadModel.mockRejectedValueOnce(new Error('Answer check failed'));
    mount([complete(CODER)]);
    await waitFor(() => expect(coderRadio()).toBeChecked());
    fireEvent.click(screen.getByRole('button', { name: /Load model/ }));
    expect(await screen.findByText('Failed to load model')).toBeInTheDocument();
    expect(screen.getByText('Answer check failed')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(loadModel).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText('Failed to load model')).not.toBeInTheDocument());
  });

  it('shows installation errors and lets the member retry', async () => {
    installModel.mockRejectedValueOnce(new Error('Disk is full'));
    mount();
    await waitFor(() => expect(coderRadio()).toBeChecked());
    fireEvent.click(downloadButton());
    expect(await screen.findByText('Disk is full')).toBeInTheDocument();
    expect(loadModel).not.toHaveBeenCalled();
    fireEvent.click(downloadButton());
    await waitFor(() => expect(installModel).toHaveBeenCalledTimes(2));
    expect(screen.queryByText('Disk is full')).not.toBeInTheDocument();
  });
});
