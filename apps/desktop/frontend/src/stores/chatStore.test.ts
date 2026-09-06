/**
 * Regression: createConversation must finish before appendMessage/update,
 * otherwise SQLite rejects the message with FOREIGN KEY constraint failed.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const history = vi.hoisted(() => ({
  create: vi.fn(),
  appendMessage: vi.fn(),
  update: vi.fn(),
  list: vi.fn(),
  get: vi.fn(),
  delete: vi.fn(),
  updateMessage: vi.fn(),
}));

const streamChat = vi.hoisted(() => vi.fn());
const streamNetworkJob = vi.hoisted(() => vi.fn());

vi.mock('@/lib/localApi', () => ({
  localApi: {
    history,
    streamChat,
    streamNetworkJob,
    usage: { record: vi.fn().mockResolvedValue({}) },
  },
}));

vi.mock('@/lib/sidecarPersist', () => ({
  persistUiPref: vi.fn(),
}));

vi.mock('@/stores/meshStore', () => ({
  useMeshStore: {
    getState: () => ({ peers: [] }),
  },
}));

import { useChatStore } from './chatStore';

function doneSse(): Response {
  return new Response('data: [DONE]\n\n', {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

describe('chatStore persistence and routing', () => {
  beforeEach(() => {
    history.create.mockReset();
    history.appendMessage.mockReset().mockResolvedValue({});
    history.update.mockReset().mockResolvedValue({});
    history.list.mockReset().mockResolvedValue([]);
    history.delete.mockReset().mockResolvedValue(undefined);
    streamChat.mockReset().mockResolvedValue(doneSse());
    streamNetworkJob.mockReset().mockResolvedValue(doneSse());

    useChatStore.setState({
      conversations: {},
      hydratedIds: new Set(),
      isHydrated: true,
      activeConversationId: null,
      isStreaming: false,
      streamingMessageId: null,
      abortController: null,
      routingScope: 'local',
      activeCircleId: null,
      networkMode: false,
      householdMode: false,
    });
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.clearAllMocks();
    useChatStore.getState().clearConversations();
  });

  it('waits for conversation create to resolve before appending the first message', async () => {
    const order: string[] = [];
    let resolveCreate!: (value: unknown) => void;

    history.create.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCreate = (value: unknown) => {
            order.push('create');
            resolve(value);
          };
        }),
    );
    history.appendMessage.mockImplementation(async () => {
      order.push('append');
      return {};
    });
    history.update.mockImplementation(async () => {
      order.push('update');
      return {};
    });

    // New chat: optimistic id returns immediately while create is still pending.
    const idPromise = useChatStore.getState().createConversation();
    const id = await idPromise;

    const sendPromise = useChatStore.getState().sendMessage('hello world');

    // Give the send path a turn — it must block on the pending create.
    await Promise.resolve();
    await Promise.resolve();

    expect(history.appendMessage).not.toHaveBeenCalled();
    expect(history.update).not.toHaveBeenCalled();

    resolveCreate({
      id,
      title: 'New chat',
      project: null,
      network_mode: 0,
      network_model_slug: null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      message_count: 0,
    });

    await sendPromise;

    // Allow fire-and-forget persist promises to settle.
    await vi.waitFor(() => {
      expect(history.appendMessage).toHaveBeenCalled();
    });

    expect(order[0]).toBe('create');
    expect(order.indexOf('append')).toBeGreaterThan(order.indexOf('create'));
    expect(history.appendMessage).toHaveBeenCalledWith(
      id,
      expect.objectContaining({ role: 'user', content: 'hello world' }),
    );
  });

  it('does not re-create a conversation that is already persisted on later sends', async () => {
    history.create.mockResolvedValue({
      id: 'fixed-id',
      title: 'New chat',
      project: null,
      network_mode: 0,
      network_model_slug: null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      message_count: 0,
    });

    const id = await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('first');
    await vi.waitFor(() => expect(history.appendMessage).toHaveBeenCalled());

    history.create.mockClear();
    history.appendMessage.mockClear();

    await useChatStore.getState().sendMessage('second');
    await vi.waitFor(() => expect(history.appendMessage).toHaveBeenCalled());

    expect(history.create).not.toHaveBeenCalled();
    expect(history.appendMessage).toHaveBeenCalledWith(
      id,
      expect.objectContaining({ role: 'user', content: 'second' }),
    );
  });

  it('keeps the circle selected at send time while conversation creation is pending', async () => {
    let resolveCreate!: (value: unknown) => void;
    history.create.mockImplementation(() => new Promise((resolve) => { resolveCreate = resolve; }));
    useChatStore.setState({ routingScope: 'circle', activeCircleId: 'selected-circle' });
    await useChatStore.getState().createConversation();

    const send = useChatStore.getState().sendMessage('private circle prompt');
    useChatStore.setState({ routingScope: 'cooperative', activeCircleId: null });
    resolveCreate({});
    await send;

    expect(streamNetworkJob).toHaveBeenCalledExactlyOnceWith(
      'private circle prompt',
      expect.objectContaining({ circleId: 'selected-circle' }),
    );
    expect(streamChat).not.toHaveBeenCalled();
  });

  it('fails without dispatch when circle scope has no selected circle', async () => {
    history.create.mockResolvedValue({});
    useChatStore.setState({ routingScope: 'circle', activeCircleId: null });

    await useChatStore.getState().sendMessage('private circle prompt');

    expect(streamChat).not.toHaveBeenCalled();
    expect(streamNetworkJob).not.toHaveBeenCalled();
    const state = useChatStore.getState();
    const messages = state.conversations[state.activeConversationId!]?.messages;
    expect(messages?.at(-1)).toMatchObject({ status: 'error' });
    expect(state.isStreaming).toBe(false);
  });
});
