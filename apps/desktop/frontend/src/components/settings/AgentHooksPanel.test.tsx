import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentHooksPanel } from './AgentHooksPanel';
import type { AgentConnectorStatus } from '@/types/agents';

const mocks = vi.hoisted(() => ({
  selectDirectory: vi.fn(),
  connectorStatus: vi.fn(),
  connectProvider: vi.fn(),
  disconnectProvider: vi.fn(),
}));

vi.mock('@/lib/ipc', () => ({
  ipc: {
    isElectron: true,
    selectDirectory: mocks.selectDirectory,
  },
}));

vi.mock('@/lib/localApi', () => ({
  localApi: {
    agents: {
      connectorStatus: mocks.connectorStatus,
      connectProvider: mocks.connectProvider,
      disconnectProvider: mocks.disconnectProvider,
    },
  },
}));

function status(overrides: Partial<AgentConnectorStatus> = {}): AgentConnectorStatus {
  return {
    provider: 'codex',
    project_path: '/repo',
    config_path: '/repo/.codex/hooks.json',
    state: 'not-installed',
    bridge_available: true,
    config_exists: false,
    installed_events: [],
    missing_events: ['SessionStart', 'PreToolUse'],
    stale_events: [],
    planned_connect_events: ['SessionStart', 'PreToolUse'],
    planned_disconnect_events: [],
    manual_config: '{"hooks":{}}\n',
    error: null,
    ...overrides,
  };
}

describe('AgentHooksPanel', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it('chooses a project, previews the merge, and installs Codex hooks', async () => {
    mocks.selectDirectory.mockResolvedValue('/repo');
    mocks.connectorStatus.mockResolvedValue(status());
    mocks.connectProvider.mockResolvedValue(
      status({
        state: 'installed',
        config_exists: true,
        installed_events: ['SessionStart', 'PreToolUse'],
        missing_events: [],
        planned_connect_events: [],
        planned_disconnect_events: ['PreToolUse', 'SessionStart'],
      }),
    );

    render(<AgentHooksPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'Choose project' }));

    await waitFor(() => expect(mocks.connectorStatus).toHaveBeenCalledWith('codex', '/repo'));
    expect(screen.getByText('/repo/.codex/hooks.json')).toBeInTheDocument();
    expect(screen.getByText(/create 2 metadata-only lifecycle hooks/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Connect Codex' }));

    await waitFor(() => expect(mocks.connectProvider).toHaveBeenCalledWith('codex', '/repo'));
    expect(await screen.findByText('2 lifecycle hooks installed')).toBeInTheDocument();
    expect(screen.getByText(/open/i)).toHaveTextContent('/hooks');
  });

  it('shows invalid JSON as recoverable and does not offer a write action', async () => {
    localStorage.setItem('potluck:agent-hook-project', '/repo');
    mocks.connectorStatus.mockResolvedValue(
      status({
        state: 'invalid',
        config_exists: true,
        error: 'Potluck could not read valid JSON from /repo/.codex/hooks.json',
        planned_connect_events: [],
      }),
    );

    render(<AgentHooksPanel />);

    expect(await screen.findByText('Potluck left this file unchanged')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Connect Codex' })).not.toBeInTheDocument();
    expect(mocks.connectProvider).not.toHaveBeenCalled();
  });

  it('switches provider status without forgetting the selected project', async () => {
    localStorage.setItem('potluck:agent-hook-project', '/repo');
    mocks.connectorStatus.mockImplementation((provider: string) =>
      Promise.resolve(
        status(
          provider === 'claude'
            ? {
                provider: 'claude',
                config_path: '/repo/.claude/settings.local.json',
              }
            : {},
        ),
      ),
    );

    render(<AgentHooksPanel />);
    await screen.findByText('/repo/.codex/hooks.json');
    fireEvent.click(screen.getByRole('tab', { name: 'Claude Code' }));

    expect(await screen.findByText('/repo/.claude/settings.local.json')).toBeInTheDocument();
    expect(mocks.connectorStatus).toHaveBeenLastCalledWith('claude', '/repo');
  });
});
