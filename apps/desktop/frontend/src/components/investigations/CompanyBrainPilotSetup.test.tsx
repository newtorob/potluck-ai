import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CompanyBrainPilotSetup } from './CompanyBrainPilotSetup';

const api = vi.hoisted(() => ({
  companyBrainModelSetup: vi.fn(),
  importCompanyBrainModel: vi.fn(),
  prepareCompanyBrainModel: vi.fn(),
  unloadCompanyBrainModel: vi.fn(),
}));
vi.mock('@/lib/localApi', () => ({ localApi: api }));
vi.mock('@/lib/ipc', () => ({ ipc: { platform: 'browser', selectPilotModelFile: vi.fn() } }));
function state(overrides: Record<string, unknown> = {}) {
  return {
    version: 'company-brain-model-setup-v1',
    model: {
      id: 'pilot',
      name: 'Pilot model',
      sha256: 'abc',
      size_bytes: 100,
      context_tokens: 8192,
      backend: 'llama-server',
    },
    platform_supported: true,
    installed: false,
    artifact_verified: false,
    ready: false,
    can_import: true,
    can_prepare: false,
    can_unload: false,
    active_model: null,
    issues: [],
    resources: {
      estimated_ram_bytes: 100,
      available_ram_bytes: 200,
      required_disk_bytes: 100,
      disk_free_bytes: 200,
    },
    warnings: [],
    ...overrides,
  };
}
function mount(onModelChange = vi.fn()) {
  const view = render(<CompanyBrainPilotSetup onModelChange={onModelChange} />);
  return { ...view, onModelChange };
}
beforeEach(() => {
  vi.clearAllMocks();
  api.companyBrainModelSetup.mockResolvedValue(state());
});
async function enterImport() {
  await screen.findByLabelText('Model file path');
  fireEvent.change(screen.getByLabelText('Model file path'), {
    target: { value: '/local/model.gguf' },
  });
  fireEvent.click(screen.getByRole('checkbox', { name: /Copy this supplied file/ }));
}
describe('CompanyBrainPilotSetup', () => {
  it.each(['checking', 'offline'] as const)(
    'does not show cached Ready while health is %s',
    async (connectionState) => {
      const ready = state({
        installed: true,
        artifact_verified: true,
        ready: true,
        active_model: 'pilot',
        can_prepare: true,
        can_unload: true,
      });
      api.companyBrainModelSetup.mockResolvedValue(ready);
      const onModelChange = vi.fn();
      const view = render(
        <CompanyBrainPilotSetup
          connectionState="connected"
          activeModelId="pilot"
          onModelChange={onModelChange}
        />,
      );
      await screen.findByText('Ready for local questions');
      fireEvent.click(screen.getByRole('checkbox', { name: /I want to prepare/ }));
      view.rerender(
        <CompanyBrainPilotSetup
          connectionState={connectionState}
          activeModelId="pilot"
          onModelChange={onModelChange}
        />,
      );
      expect(screen.queryByText('Ready for local questions')).not.toBeInTheDocument();
      expect(
        screen.getByText(
          connectionState === 'checking'
            ? 'Checking local engine'
            : 'Local engine offline; check connection',
        ),
      ).toBeVisible();
      expect(screen.getByRole('checkbox', { name: /I want to prepare/ })).not.toBeChecked();
      expect(screen.getByRole('button', { name: 'Prepare model' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'End model session' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Refresh setup' })).toBeEnabled();
      fireEvent.click(screen.getByRole('button', { name: 'Refresh setup' }));
      await waitFor(() => expect(api.companyBrainModelSetup).toHaveBeenCalledTimes(2));
      expect(api.prepareCompanyBrainModel).not.toHaveBeenCalled();
      expect(api.unloadCompanyBrainModel).not.toHaveBeenCalled();
      expect(onModelChange).not.toHaveBeenCalled();
    },
  );
  it('invalidates stale readiness and consent after the runtime unloads, then reconciles on refresh', async () => {
    api.companyBrainModelSetup.mockResolvedValueOnce(
      state({
        installed: true,
        artifact_verified: true,
        ready: true,
        active_model: 'pilot',
        can_prepare: true,
        can_unload: true,
      }),
    );
    const onModelChange = vi.fn();
    const view = render(
      <CompanyBrainPilotSetup activeModelId="pilot" onModelChange={onModelChange} />,
    );
    await screen.findByText('Ready for local questions');
    const consent = screen.getByRole('checkbox', { name: /I want to prepare/ });
    fireEvent.click(consent);
    expect(consent).toBeChecked();
    view.rerender(<CompanyBrainPilotSetup activeModelId={null} onModelChange={onModelChange} />);
    expect(screen.queryByText('Ready for local questions')).not.toBeInTheDocument();
    expect(screen.getByText('Model state changed; refresh setup')).toBeVisible();
    expect(consent).not.toBeChecked();
    expect(consent).toBeDisabled();
    expect(screen.getByRole('button', { name: 'End model session' })).toBeDisabled();
    api.companyBrainModelSetup.mockResolvedValueOnce(
      state({ installed: true, artifact_verified: true, can_prepare: true }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh setup' }));
    await screen.findByText('Model imported; not ready');
    expect(screen.queryByText('Model state changed; refresh setup')).not.toBeInTheDocument();
    expect(consent).toBeEnabled();
    expect(consent).not.toBeChecked();
    expect(api.prepareCompanyBrainModel).not.toHaveBeenCalled();
    expect(api.unloadCompanyBrainModel).not.toHaveBeenCalled();
    expect(api.importCompanyBrainModel).not.toHaveBeenCalled();
    expect(onModelChange).not.toHaveBeenCalled();
  });
  it('only reads setup on mount and requires explicit import consent', async () => {
    mount();
    await screen.findByLabelText('Model file path');
    expect(api.companyBrainModelSetup).toHaveBeenCalledTimes(1);
    expect(api.importCompanyBrainModel).not.toHaveBeenCalled();
    expect(api.prepareCompanyBrainModel).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Model file path'), {
      target: { value: '/local/model.gguf' },
    });
    expect(screen.getByRole('button', { name: 'Import model' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /Copy this supplied file/ }));
    api.importCompanyBrainModel.mockResolvedValue(
      state({ installed: true, artifact_verified: true, can_prepare: true }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Import model' }));
    await waitFor(() =>
      expect(api.importCompanyBrainModel).toHaveBeenCalledWith({
        source_path: '/local/model.gguf',
        import_confirmed: true,
      }),
    );
    expect(api.prepareCompanyBrainModel).not.toHaveBeenCalled();
  });
  it('preserves import entries on failure and blocks duplicate activations', async () => {
    let reject!: (error: Error) => void;
    api.importCompanyBrainModel.mockImplementation(
      () =>
        new Promise((_, fail) => {
          reject = fail;
        }),
    );
    mount();
    await enterImport();
    const button = screen.getByRole('button', { name: 'Import model' });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(api.importCompanyBrainModel).toHaveBeenCalledTimes(1);
    reject(new Error('Import failed'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Import failed');
    expect(screen.getByLabelText('Model file path')).toHaveValue('/local/model.gguf');
    expect(screen.getByRole('checkbox', { name: /Copy this supplied file/ })).toBeChecked();
    expect(api.companyBrainModelSetup).toHaveBeenCalledTimes(1);
  });
  it('requires prepare consent and passes active identity; unload notifies only after confirmation', async () => {
    api.companyBrainModelSetup.mockResolvedValue(
      state({ installed: true, artifact_verified: true, can_prepare: true, active_model: 'old' }),
    );
    api.prepareCompanyBrainModel.mockResolvedValue(
      state({
        installed: true,
        artifact_verified: true,
        ready: true,
        can_unload: true,
        active_model: 'pilot',
      }),
    );
    api.unloadCompanyBrainModel.mockResolvedValue(
      state({ installed: true, artifact_verified: true, can_prepare: true, active_model: null }),
    );
    const { onModelChange } = mount();
    await screen.findByText(/replaces the currently loaded model/);
    expect(screen.getByRole('button', { name: 'Prepare model' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /prepare this model/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Prepare model' }));
    await waitFor(() => expect(onModelChange).toHaveBeenCalledWith('pilot'));
    expect(api.prepareCompanyBrainModel).toHaveBeenCalledWith({
      prepare_confirmed: true,
      expected_active_model: 'old',
    });
    fireEvent.click(screen.getByRole('button', { name: 'End model session' }));
    await waitFor(() => expect(onModelChange).toHaveBeenCalledWith(null));
    expect(api.unloadCompanyBrainModel).toHaveBeenCalledWith({ expected_active_model: 'pilot' });
  });
  it('does not mark a non-ready response ready and retains consent on failed prepare', async () => {
    api.companyBrainModelSetup.mockResolvedValue(
      state({ installed: true, artifact_verified: true, can_prepare: true }),
    );
    api.prepareCompanyBrainModel.mockRejectedValue(new Error('Resources changed'));
    const { onModelChange } = mount();
    await screen.findByText('Model imported; not ready');
    fireEvent.click(screen.getByRole('checkbox', { name: /prepare this model/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Prepare model' }));
    await screen.findByRole('alert');
    expect(screen.getByRole('checkbox', { name: /prepare this model/ })).toBeChecked();
    expect(onModelChange).not.toHaveBeenCalled();
  });
  it('disables unavailable actions and shows unsupported platform issue', async () => {
    api.companyBrainModelSetup.mockResolvedValue(
      state({
        platform_supported: false,
        can_import: false,
        can_prepare: false,
        issues: [
          {
            code: 'platform',
            message: 'Install a supported build.',
            blocks: ['import', 'prepare'],
          },
        ],
      }),
    );
    mount();
    expect(await screen.findByText('Install a supported build.')).toBeInTheDocument();
    expect(screen.getByText(/This platform is not supported/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Import model' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Prepare model' })).toBeDisabled();
  });
  it('ignores a late prepare after unmount', async () => {
    let resolve!: (value: unknown) => void;
    api.companyBrainModelSetup.mockResolvedValue(
      state({ installed: true, artifact_verified: true, can_prepare: true }),
    );
    api.prepareCompanyBrainModel.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const { unmount, onModelChange } = mount();
    await screen.findByText('Model imported; not ready');
    fireEvent.click(screen.getByRole('checkbox', { name: /prepare this model/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Prepare model' }));
    unmount();
    resolve(state({ ready: true, active_model: 'pilot' }));
    await Promise.resolve();
    expect(onModelChange).not.toHaveBeenCalled();
  });
  it('shows a present unverified file and resets replacement consent when active model changes on refresh', async () => {
    api.companyBrainModelSetup
      .mockResolvedValueOnce(
        state({
          installed: true,
          artifact_verified: false,
          can_prepare: true,
          active_model: 'old',
        }),
      )
      .mockResolvedValueOnce(
        state({
          installed: true,
          artifact_verified: false,
          can_prepare: true,
          active_model: 'new',
        }),
      );
    mount();
    expect(
      await screen.findByText('Model file present; preparation verifies it'),
    ).toBeInTheDocument();
    const consent = screen.getByRole('checkbox', { name: /prepare this model/ });
    fireEvent.click(consent);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh setup' }));
    await screen.findByText(/currently loaded model \(new\)/);
    expect(consent).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Prepare model' })).toBeDisabled();
  });
  it('reports mutation busy once and completes even if parent disables it; unmount clears busy', async () => {
    let resolve!: (value: unknown) => void;
    api.companyBrainModelSetup.mockResolvedValue(
      state({ installed: true, artifact_verified: true, can_prepare: true }),
    );
    api.prepareCompanyBrainModel.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const onBusyChange = vi.fn(),
      onModelChange = vi.fn();
    const view = render(
      <CompanyBrainPilotSetup onModelChange={onModelChange} onBusyChange={onBusyChange} />,
    );
    await screen.findByText('Model imported; not ready');
    expect(onBusyChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('checkbox', { name: /prepare this model/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Prepare model' }));
    expect(onBusyChange.mock.calls).toEqual([[true]]);
    view.rerender(
      <CompanyBrainPilotSetup disabled onModelChange={onModelChange} onBusyChange={onBusyChange} />,
    );
    resolve(
      state({ installed: true, artifact_verified: true, ready: true, active_model: 'pilot' }),
    );
    await waitFor(() => expect(onModelChange).toHaveBeenCalledWith('pilot'));
    expect(onBusyChange.mock.calls).toEqual([[true], [false]]);
    view.unmount();
    expect(onBusyChange.mock.calls).toEqual([[true], [false]]);
  });
  it('clears parent busy on unmount without a duplicate callback after late completion', async () => {
    let resolve!: (value: unknown) => void;
    api.companyBrainModelSetup.mockResolvedValue(
      state({ installed: true, artifact_verified: true, can_prepare: true }),
    );
    api.prepareCompanyBrainModel.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const onBusyChange = vi.fn();
    const view = render(
      <CompanyBrainPilotSetup onModelChange={vi.fn()} onBusyChange={onBusyChange} />,
    );
    await screen.findByText('Model imported; not ready');
    fireEvent.click(screen.getByRole('checkbox', { name: /prepare this model/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Prepare model' }));
    view.unmount();
    expect(onBusyChange.mock.calls).toEqual([[true], [false]]);
    resolve(state({ ready: true }));
    await Promise.resolve();
    expect(onBusyChange.mock.calls).toEqual([[true], [false]]);
  });
});
