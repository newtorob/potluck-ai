import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentRunsPanel } from './AgentRunsPanel';
import type { AgentRunDetail } from '@/types/agents';

const selectRun = vi.fn();
const selectEvent = vi.fn();
const navigate = vi.fn();

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigate,
}));

const detail: AgentRunDetail = {
  run: {
    id: 'run-1',
    provider: 'codex',
    provider_session_id: 'session-1',
    project: 'potluckai',
    model: 'gpt-5.6',
    status: 'active',
    capture_profile: 'metadata-only',
    capture_note: 'Partial hook coverage.',
    control_mode: 'observed',
    task_id: null,
    cwd: null,
    started_at: '2026-09-03T12:00:00.000Z',
    ended_at: null,
    last_event_at: '2026-09-03T12:00:02.000Z',
    agent_count: 2,
    event_count: 4,
    error_count: 0,
  },
  instances: [
    {
      id: 'root',
      run_id: 'run-1',
      provider_agent_id: 'root',
      parent_instance_id: null,
      kind: 'root',
      label: 'Root agent',
      model: 'gpt-5.6',
      status: 'active',
      started_at: '2026-09-03T12:00:00.000Z',
      ended_at: null,
    },
    {
      id: 'reviewer',
      run_id: 'run-1',
      provider_agent_id: 'agent-reviewer',
      parent_instance_id: 'root',
      kind: 'subagent',
      label: 'Reviewer',
      model: 'gpt-5.6',
      status: 'active',
      started_at: '2026-09-03T12:00:00.500Z',
      ended_at: null,
    },
  ],
  events: [
    {
      seq: 1,
      id: 'run-start',
      run_id: 'run-1',
      agent_instance_id: 'root',
      event_type: 'run.started',
      provider_event_name: 'SessionStart',
      correlation_id: null,
      turn_id: null,
      tool_name: null,
      status: 'started',
      occurred_at: '2026-09-03T12:00:00.000Z',
      received_at: '2026-09-03T12:00:00.000Z',
      duration_ms: null,
      attribution_confidence: 'exact',
      metadata: {},
    },
    {
      seq: 2,
      id: 'agent-start',
      run_id: 'run-1',
      agent_instance_id: 'reviewer',
      event_type: 'agent.started',
      provider_event_name: 'SubagentStart',
      correlation_id: 'agent-reviewer',
      turn_id: 'turn-1',
      tool_name: null,
      status: 'started',
      occurred_at: '2026-09-03T12:00:00.500Z',
      received_at: '2026-09-03T12:00:00.500Z',
      duration_ms: null,
      attribution_confidence: 'exact',
      metadata: { agent_type: 'Reviewer' },
    },
    {
      seq: 3,
      id: 'tool-start',
      run_id: 'run-1',
      agent_instance_id: null,
      event_type: 'tool.started',
      provider_event_name: 'PreToolUse',
      correlation_id: 'tool-1',
      turn_id: 'turn-1',
      tool_name: 'Bash',
      status: 'started',
      occurred_at: '2026-09-03T12:00:01.000Z',
      received_at: '2026-09-03T12:00:01.000Z',
      duration_ms: null,
      attribution_confidence: 'unknown',
      metadata: { tool_category: 'shell' },
    },
    {
      seq: 4,
      id: 'tool-complete',
      run_id: 'run-1',
      agent_instance_id: null,
      event_type: 'tool.completed',
      provider_event_name: 'PostToolUse',
      correlation_id: 'tool-1',
      turn_id: 'turn-1',
      tool_name: 'Bash',
      status: 'completed',
      occurred_at: '2026-09-03T12:00:02.000Z',
      received_at: '2026-09-03T12:00:02.000Z',
      duration_ms: 1_000,
      attribution_confidence: 'unknown',
      metadata: { tool_category: 'shell' },
    },
  ],
  resources: [],
  next_cursor: 4,
};

vi.mock('@/hooks/useAgentRuns', () => ({
  useAgentRuns: () => ({
    runs: [detail.run],
    selectedRunId: detail.run.id,
    detail,
    selectedEventId: 'tool-complete',
    isFetching: false,
    lastError: null,
    selectRun,
    selectEvent,
  }),
}));

describe('AgentRunsPanel', () => {
  beforeEach(() => {
    selectRun.mockClear();
    selectEvent.mockClear();
    navigate.mockClear();
  });

  it('renders root, subagent, and unattributed activity lanes', () => {
    render(<AgentRunsPanel />);

    expect(screen.getByText('Root agent')).toBeInTheDocument();
    expect(screen.getByText('Reviewer')).toBeInTheDocument();
    expect(screen.getByText('Shared activity')).toBeInTheDocument();
    expect(screen.getByText('Partial capture')).toBeInTheDocument();
    expect(screen.getByText('unknown')).toBeInTheDocument();
  });

  it('selects a paired tool event for inspection', () => {
    render(<AgentRunsPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'Inspect Bash' }));

    expect(selectEvent).toHaveBeenCalledWith(detail.events[3]);
  });
});
