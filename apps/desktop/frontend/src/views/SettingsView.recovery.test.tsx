import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/localApi', () => ({
  localApi: { getSettings: vi.fn(), updateSettings: vi.fn() },
}));

// Exercise the real view, hook, store and recovery controls. Panel internals
// have their own APIs; replace them with a setting value and a local draft so
// these tests can detect whether background recovery discards cached content.
function SettingsPanel({ settings }: { settings: PotluckSettings }) {
  return (
    <div>
      <p>Loaded theme: {settings.app.theme}</p>
      <input aria-label="Unsaved panel draft" defaultValue="" />
    </div>
  );
}
vi.mock('@/components/settings/HardwarePanel', () => ({ HardwarePanel: SettingsPanel }));
vi.mock('@/components/settings/PrivacyPanel', () => ({ PrivacyPanel: SettingsPanel }));
vi.mock('@/components/settings/VaultPanel', () => ({ VaultPanel: SettingsPanel }));
vi.mock('@/components/settings/GatewayPanel', () => ({ GatewayPanel: SettingsPanel }));
vi.mock('@/components/settings/AdvancedPanel', () => ({ AdvancedPanel: SettingsPanel }));
vi.mock('@/components/settings/AccountPanel', () => ({
  AccountPanel: () => <p>Account controls remain available</p>,
}));

import { localApi, type PotluckSettings } from '@/lib/localApi';
import { useSettingsStore } from '@/stores/settingsStore';
import { SettingsView } from './SettingsView';

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
  useSettingsStore.setState(useSettingsStore.getInitialState(), true);
});

afterEach(async () => {
  await act(async () => {
    unfinished.splice(0).forEach((finish) => finish());
  });
});

function openSettings(tab: string) {
  return render(
    <MemoryRouter initialEntries={[`/settings?tab=${tab}`]}>
      <SettingsView />
    </MemoryRouter>,
  );
}

describe('SettingsView loading recovery through the real hook and store', () => {
  it.each([
    ['hardware', 'Hardware'],
    ['privacy', 'Privacy'],
    ['vault', 'Vault'],
    ['connect', 'Connect tools'],
    ['advanced', 'Advanced'],
  ])('%s replaces failed loading with Retry and recovers the same tab', async (tab, label) => {
    const user = userEvent.setup();
    const initial = response();
    vi.mocked(localApi.getSettings).mockReturnValueOnce(initial.promise);
    openSettings(tab);
    const panel = screen.getByRole('tabpanel', { name: label });
    expect(within(panel).getByLabelText('Loading settings')).toBeVisible();

    await act(async () => {
      initial.reject(new Error('http://private-internal-host:8321/settings?api_key=secret'));
    });
    expect(within(panel).queryByLabelText('Loading settings')).not.toBeInTheDocument();
    expect(within(panel).getByRole('alert')).toHaveTextContent('Settings couldn’t load');
    expect(screen.queryByText(/private-internal-host|api_key=secret/)).not.toBeInTheDocument();
    const retryButton = within(panel).getByRole('button', { name: 'Retry' });
    expect(retryButton).toBeEnabled();

    const retry = response();
    vi.mocked(localApi.getSettings).mockReturnValueOnce(retry.promise);
    await user.click(retryButton);
    const busy = within(panel).getByRole('button', { name: 'Trying again…' });
    expect(busy).toBe(retryButton);
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute('aria-busy', 'true');
    expect(within(panel).getByRole('alert')).toHaveTextContent('Settings couldn’t load');
    await user.click(busy);
    expect(localApi.getSettings).toHaveBeenCalledTimes(2);

    await act(async () => {
      retry.resolve(settings('dark'));
    });
    expect(screen.getByRole('tab', { name: label })).toHaveAttribute('aria-selected', 'true');
    expect(within(panel).getByText('Loaded theme: dark')).toBeVisible();
    expect(within(panel).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(within(panel).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps cached settings and local panel input while refresh fails and retries', async () => {
    const user = userEvent.setup();
    useSettingsStore.getState().hydrateFromSidecar(settings('light'));
    const refresh = response();
    vi.mocked(localApi.getSettings).mockReturnValueOnce(refresh.promise);
    openSettings('privacy');
    const panel = screen.getByRole('tabpanel', { name: 'Privacy' });
    const draft = within(panel).getByRole('textbox', { name: 'Unsaved panel draft' });
    await user.type(draft, 'keep my draft');
    await act(async () => {
      refresh.reject(new Error('offline'));
    });
    expect(within(panel).getByRole('status')).toHaveTextContent('Couldn’t refresh settings');
    expect(within(panel).getByRole('status')).toHaveTextContent(
      'Showing your last loaded settings',
    );
    expect(within(panel).getByText('Loaded theme: light')).toBeVisible();
    expect(draft).toHaveValue('keep my draft');
    expect(within(panel).getByRole('textbox', { name: 'Unsaved panel draft' })).toBe(draft);

    const retry = response();
    vi.mocked(localApi.getSettings).mockReturnValueOnce(retry.promise);
    await user.click(within(panel).getByRole('button', { name: 'Retry' }));
    expect(within(panel).getByRole('button', { name: 'Trying again…' })).toBeDisabled();
    expect(within(panel).getByText('Loaded theme: light')).toBeVisible();
    expect(within(panel).getByRole('textbox', { name: 'Unsaved panel draft' })).toBe(draft);
    expect(draft).toHaveValue('keep my draft');

    await act(async () => {
      retry.resolve(settings('dark'));
    });
    expect(within(panel).getByText('Loaded theme: dark')).toBeVisible();
    expect(within(panel).queryByRole('status')).not.toBeInTheDocument();
    expect(within(panel).getByRole('textbox', { name: 'Unsaved panel draft' })).toBe(draft);
    expect(draft).toHaveValue('keep my draft');
  });

  it('restores Retry after another failure and allows a keyboard retry', async () => {
    const user = userEvent.setup();
    vi.mocked(localApi.getSettings).mockRejectedValueOnce(new Error('offline'));
    openSettings('hardware');
    await act(async () => {});
    const hardware = screen.getByRole('tab', { name: 'Hardware' });
    hardware.focus();
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: 'Privacy' })).toHaveFocus();
    expect(screen.getByRole('tab', { name: 'Privacy' })).toHaveAttribute('aria-selected', 'true');
    await user.tab();
    const retry = screen.getByRole('button', { name: 'Retry' });
    expect(retry).toHaveFocus();
    vi.mocked(localApi.getSettings).mockRejectedValueOnce(new Error('still offline'));
    await user.keyboard('{Enter}');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Trying again…' })).not.toBeInTheDocument();
    expect(localApi.getSettings).toHaveBeenCalledTimes(2);

    vi.mocked(localApi.getSettings).mockResolvedValueOnce(settings());
    await user.keyboard('{Enter}');
    const panel = screen.getByRole('tabpanel', { name: 'Privacy' });
    expect(within(panel).getByText('Loaded theme: light')).toBeVisible();
    expect(localApi.getSettings).toHaveBeenCalledTimes(3);
  });

  it('keeps Account available when settings could not load', async () => {
    vi.mocked(localApi.getSettings).mockRejectedValueOnce(new Error('offline'));
    openSettings('account');
    await act(async () => {});
    expect(screen.getByText('Account controls remain available')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });
});
