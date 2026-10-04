import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { HouseholdStreamOptions } from '@/lib/householdInference';

const mocks = vi.hoisted(() => ({
  household: vi.fn(), network: vi.fn(), local: vi.fn(),
  history: { create: vi.fn(), appendMessage: vi.fn(), update: vi.fn() },
  usage: vi.fn(), settings: { privacy: { chat_history_enabled: false } },
}));
vi.mock('@/lib/localApi', () => ({ localApi: {
  streamChat: mocks.local, streamNetworkJob: mocks.network,
  history: mocks.history, usage: { record: mocks.usage },
} }));
vi.mock('@/lib/sidecarPersist', () => ({ persistUiPref: vi.fn() }));
vi.mock('@/lib/householdInference', () => ({ streamHouseholdInference: mocks.household }));
vi.mock('@/lib/meshDiagnostics', () => ({ getConnectedPeers: () => [] }));
vi.mock('@/stores/meshStore', () => ({ useMeshStore: { getState: () => ({ peers: [] }) } }));
vi.mock('@/stores/settingsStore', () => ({ useSettingsStore: { getState: () => ({ settings: mocks.settings }) } }));

import { useChatStore } from './chatStore';

const peer = { node_id: 'p1', node_name: 'Computer', mesh_ip: '100.64.0.2' };
const first = 'data: {"choices":[{"delta":{"content":"unfinished answer"}}]}\n\n';
const completed = 'data: {"choices":[{"delta":{"content":"replacement"}}]}\n\ndata: [DONE]\n\n';

function active() {
  const state = useChatStore.getState();
  return state.conversations[state.activeConversationId!]!;
}
async function interrupt() {
  mocks.household.mockImplementationOnce(async (_messages, _peers, options: HouseholdStreamOptions) => {
    options.onAttempt?.(peer as never, 'original-model');
    return { response: new Response(first), peer, capabilities: {} };
  });
  await useChatStore.getState().sendMessage('original request');
  return active().messages.at(-1)!;
}

beforeEach(() => {
  useChatStore.getState().clearConversations();
  vi.clearAllMocks();
  mocks.settings.privacy.chat_history_enabled = false;
  mocks.history.create.mockResolvedValue({});
  mocks.history.appendMessage.mockResolvedValue({});
  mocks.history.update.mockResolvedValue({});
  mocks.usage.mockResolvedValue({});
  useChatStore.setState({ routingScope: 'household', networkMode: false, householdMode: true,
    networkModelSlug: 'unused-pool-model', routingMode: 'capability' });
});

describe('explicit household restart', () => {
  it('preserves the partial answer and restarts the original request within My Machines', async () => {
    const interrupted = await interrupt();
    expect(interrupted.status).toBe('error');
    expect(interrupted.content).toBe('unfinished answer');
    const originalInput = mocks.household.mock.calls[0]![0];
    // New settings and messages must not rewrite the failed request.
    useChatStore.getState().setRoutingScope('cooperative');
    useChatStore.getState().setNetworkModelSlug('changed-model');
    useChatStore.getState().setRoutingMode('round_robin');
    mocks.household.mockImplementationOnce(async (_messages, _peers, options: HouseholdStreamOptions) => {
      options.onAttempt?.({ ...peer, node_id: 'p2' } as never, 'original-model');
      return { response: new Response(completed), peer, capabilities: {} };
    });
    await useChatStore.getState().restartMessage(interrupted.id);
    const messages = active().messages;
    expect(messages.map((m) => m.content)).toEqual(['original request', 'unfinished answer', 'replacement']);
    expect(messages[1]?.status).toBe('error');
    expect(messages[1]?.recovery).toBeUndefined();
    expect(messages[2]?.status).toBe('complete');
    expect(messages[2]?.restartOf).toBe(interrupted.id);
    expect(messages[2]?.recovery).toBeUndefined();
    expect(mocks.household.mock.calls[1]![0]).toEqual(originalInput);
    expect(mocks.household.mock.calls[1]![2]).toMatchObject({ modelSlug: 'original-model',
      excludedPeerIds: ['p1'], routingMode: 'capability' });
    expect(mocks.network).not.toHaveBeenCalled();
    expect(mocks.local).not.toHaveBeenCalled();
  });

  it('does not start two replacements on a double click', async () => {
    const interrupted = await interrupt();
    mocks.household.mockResolvedValue({ response: new Response(completed), peer });
    await Promise.all([useChatStore.getState().restartMessage(interrupted.id),
      useChatStore.getState().restartMessage(interrupted.id)]);
    expect(mocks.household).toHaveBeenCalledTimes(2); // original + one replacement
  });

  it('retains the snapshot if no replacement can currently serve it', async () => {
    const interrupted = await interrupt();
    mocks.household.mockRejectedValueOnce(new Error('No compatible computer available'));
    await useChatStore.getState().restartMessage(interrupted.id);
    const failedReplacement = active().messages.at(-1)!;
    expect(failedReplacement.status).toBe('error');
    expect(failedReplacement.recovery?.modelSlug).toBe('original-model');
    expect(failedReplacement.recovery?.excludedPeerIds).toEqual(['p1']);
  });

  it('keeps history-off requests ephemeral even if history is enabled before restart', async () => {
    const interrupted = await interrupt();
    mocks.settings.privacy.chat_history_enabled = true;
    mocks.household.mockResolvedValueOnce({ response: new Response(completed), peer });
    await useChatStore.getState().restartMessage(interrupted.id);
    expect(mocks.history.create).not.toHaveBeenCalled();
    expect(mocks.history.appendMessage).not.toHaveBeenCalled();
    expect(mocks.history.update).not.toHaveBeenCalled();
  });

  it('does not dispatch for a completed or missing message', async () => {
    await useChatStore.getState().restartMessage('missing');
    expect(mocks.household).not.toHaveBeenCalled();
  });
});
