import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { TaskWorkroom } from './TaskWorkroom';
import type { ManagedAgentSession } from '@/types/agents';
import type { TaskState } from '@/types/tasks';

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  selectDirectory: vi.fn(),
  capabilities: vi.fn(),
  listSessions: vi.fn(),
  startSession: vi.fn(),
  getSession: vi.fn(),
  sendMessage: vi.fn(),
  steer: vi.fn(),
  interrupt: vi.fn(),
  resolveApproval: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => mocks.navigate,
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
      managedCapabilities: mocks.capabilities,
      listManagedSessions: mocks.listSessions,
      startManagedSession: mocks.startSession,
      getManagedSession: mocks.getSession,
      sendManagedMessage: mocks.sendMessage,
      steerManagedSession: mocks.steer,
      interruptManagedSession: mocks.interrupt,
      resolveManagedApproval: mocks.resolveApproval,
    },
  },
}));

const task: TaskState = {
  id: 'task-1',
  task_name: 'Build the workroom',
  status: 'active',
  objective: 'Control Codex from Potluck',
  completed_steps: [],
  pending_steps: ['Build launch flow'],
  known_constraints: [],
  context_summary: '',
  files_touched: [],
  errors_encountered: [],
  metadata: {},
  created_at: '2026-09-05T12:00:00.000Z',
  updated_at: '2026-09-05T12:00:00.000Z',
  conversation_id: null,
  project: 'potluck',
};

function session(overrides: Partial<ManagedAgentSession> = {}): ManagedAgentSession {
  return {
    id: 'thread-1',
    run_id: 'run-1',
    provider: 'codex',
    control_mode: 'managed',
    task_id: task.id,
    task_name: task.task_name,
    cwd: '/repo',
    project: 'potluck',
    model: 'gpt-5.6',
    sandbox: 'workspace-write',
    status: 'running',
    active_turn_id: 'turn-1',
    started_at: '2026-09-05T12:00:00.000Z',
    updated_at: '2026-09-05T12:00:01.000Z',
    error: null,
    messages: [],
    items: [],
    approvals: [],
    capabilities: ['messages', 'steer', 'interrupt', 'approvals', 'resume'],
    ...overrides,
  };
}

describe('TaskWorkroom', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mocks.capabilities.mockResolvedValue({
      provider: 'codex',
      available: true,
      version: 'codex-cli 0.153.2',
      features: ['messages', 'steer', 'interrupt', 'approvals', 'resume'],
      error: null,
    });
    mocks.listSessions.mockResolvedValue([]);
    mocks.selectDirectory.mockResolvedValue('/repo');
    mocks.getSession.mockImplementation(() => new Promise(() => {}));
  });

  it('chooses a repository and launches Codex from the task', async () => {
    mocks.startSession.mockResolvedValue(session());
    render(<TaskWorkroom task={task} onTaskRefresh={vi.fn()} />);

    expect(await screen.findByText('Start a Codex workroom')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /choose a repository/i }));
    await screen.findByText('/repo');
    fireEvent.change(screen.getByLabelText('Starting direction (optional)'), {
      target: { value: 'Begin with the UI.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Start Codex' }));

    await waitFor(() => expect(mocks.startSession).toHaveBeenCalledWith({
      task_id: 'task-1',
      cwd: '/repo',
      sandbox: 'workspace-write',
      direction: 'Begin with the UI.',
    }));
    expect(await screen.findByText('Managed by Potluck')).toBeInTheDocument();
  });

  it('renders an approval and returns the selected decision to Codex', async () => {
    const waiting = session({
      status: 'waiting_approval',
      approvals: [{
        id: 'approval-1',
        kind: 'command',
        title: 'Run command',
        thread_id: 'thread-1',
        turn_id: 'turn-1',
        item_id: 'item-1',
        requested_at: '2026-09-05T12:00:02.000Z',
        command: 'npm test',
        cwd: '/repo',
        reason: 'Run the focused test suite',
        available_decisions: ['accept', 'decline'],
      }],
    });
    mocks.listSessions.mockResolvedValue([waiting]);
    mocks.resolveApproval.mockResolvedValue(session());
    render(<TaskWorkroom task={task} onTaskRefresh={vi.fn()} />);

    expect(await screen.findByText('npm test')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }));

    await waitFor(() => expect(mocks.resolveApproval).toHaveBeenCalledWith(
      'thread-1',
      'approval-1',
      'accept',
    ));
  });
});
