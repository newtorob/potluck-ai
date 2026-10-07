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
const usageRecord = vi.hoisted(() => vi.fn());

vi.mock('@/lib/localApi', () => ({
  localApi: {
    history,
    streamChat,
    streamNetworkJob,
    usage: { record: usageRecord },
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

// Privacy gate: settings.privacy.chat_history_enabled. Mutable per-test so a
// case can flip the flag (including mid-stream). `settings: null` models the
// pre-hydration state, which must fail closed.
const settingsMock = vi.hoisted(() => ({
  settings: null as { privacy: { chat_history_enabled: boolean } } | null,
}));

vi.mock('@/stores/settingsStore', () => ({
  useSettingsStore: {
    getState: () => settingsMock,
  },
}));

function setChatHistoryEnabled(enabled: boolean): void {
  settingsMock.settings = { privacy: { chat_history_enabled: enabled } };
}

import { reconcileCircleSelection, useChatStore } from './chatStore';

function doneSse(): Response {
  return new Response('data: [DONE]\n\n', {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

/** A finished stream whose whole body is known before the send starts. */
function sse(body: string): Response {
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

/** A stream the test drives by hand, so a case can act (flip a setting,
 *  abort) while the assistant reply is still in flight. */
function controllableSse(): {
  response: Response;
  push: (chunk: string) => void;
  close: () => void;
} {
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      ctrl = c;
    },
  });
  return {
    response: new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }),
    push: (chunk: string) => ctrl.enqueue(encoder.encode(chunk)),
    close: () => {
      try {
        ctrl.close();
      } catch {
        /* already closed */
      }
    },
  };
}

/** Let every already-scheduled fire-and-forget persist microtask run. */
async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('chatStore persistence and routing', () => {
  beforeEach(() => {
    history.create.mockReset().mockResolvedValue({});
    history.appendMessage.mockReset().mockResolvedValue({});
    history.update.mockReset().mockResolvedValue({});
    history.list.mockReset().mockResolvedValue([]);
    history.delete.mockReset().mockResolvedValue(undefined);
    streamChat.mockReset().mockResolvedValue(doneSse());
    streamNetworkJob.mockReset().mockResolvedValue(doneSse());
    usageRecord.mockReset().mockResolvedValue({ ok: true });
    setChatHistoryEnabled(true);

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

  it('sends a circle the conversation so far and the open pool only the prompt', async () => {
    history.create.mockResolvedValue({});
    useChatStore.setState({ routingScope: 'circle', activeCircleId: 'selected-circle' });
    await useChatStore.getState().sendMessage('first question');
    expect(streamNetworkJob.mock.calls[0]?.[1]).toMatchObject({
      messages: [{ role: 'user', content: 'first question' }],
    });

    streamNetworkJob.mockClear();
    useChatStore.setState({ routingScope: 'cooperative', activeCircleId: null });
    await useChatStore.getState().sendMessage('second question');
    expect(streamNetworkJob.mock.calls[0]?.[1]).not.toHaveProperty('messages');
  });

  it('remembers the chosen circle and forgets it when cleared', () => {
    useChatStore.getState().setActiveCircleId('circle-a');
    expect(window.localStorage.getItem('potluck.active_circle_id')).toBe('circle-a');
    useChatStore.getState().setActiveCircleId(null);
    expect(window.localStorage.getItem('potluck.active_circle_id')).toBeNull();
  });

  it('resolves a remembered circle against the circles that still exist', () => {
    const a = { id: 'a' };
    const b = { id: 'b' };
    expect(reconcileCircleSelection('a', [a, b])).toBe('a');
    expect(reconcileCircleSelection('gone', [a, b])).toBeNull();
    expect(reconcileCircleSelection('gone', [b])).toBe('b');
    expect(reconcileCircleSelection(null, [a])).toBe('a');
    expect(reconcileCircleSelection(null, [])).toBeNull();
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
  // -------------------------------------------------------------------------
  // Privacy: settings.privacy.chat_history_enabled gates every content write
  // -------------------------------------------------------------------------

  describe('chat history disabled', () => {
    it('completes a send fully in memory and writes nothing', async () => {
      setChatHistoryEnabled(false);

      const id = await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello world');
      await flush();

      expect(history.create).not.toHaveBeenCalled();
      expect(history.appendMessage).not.toHaveBeenCalled();
      expect(history.update).not.toHaveBeenCalled();

      const state = useChatStore.getState();
      const convo = state.conversations[id];
      expect(convo?.messages.map((m) => m.role)).toEqual(['user', 'assistant']);
      expect(convo?.messages[0]?.content).toBe('hello world');
      expect(convo?.title).toBe('hello world');
      expect(state.isStreaming).toBe(false);
    });

    it('fails closed before settings have hydrated', async () => {
      settingsMock.settings = null;

      await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello world');
      await flush();

      expect(history.create).not.toHaveBeenCalled();
      expect(history.appendMessage).not.toHaveBeenCalled();
      expect(history.update).not.toHaveBeenCalled();
    });

    it('keeps the conversation in the list marked not saved and never fetches it', async () => {
      setChatHistoryEnabled(false);

      const id = await useChatStore.getState().createConversation();
      expect(useChatStore.getState().conversations[id]?.persisted).toBe(false);

      await useChatStore.getState().switchConversation(id);

      expect(Object.keys(useChatStore.getState().conversations)).toContain(id);
      expect(useChatStore.getState().activeConversationId).toBe(id);
      expect(history.get).not.toHaveBeenCalled();
    });

    it('marks a conversation created while history is on as saved', async () => {
      setChatHistoryEnabled(true);

      const id = await useChatStore.getState().createConversation();

      expect(useChatStore.getState().conversations[id]?.persisted).toBe(true);
    });

    it('persists nothing on abort', async () => {
      setChatHistoryEnabled(false);
      const stream = controllableSse();
      streamChat.mockResolvedValue(stream.response);

      const id = await useChatStore.getState().createConversation();
      const send = useChatStore.getState().sendMessage('hello');
      await vi.waitFor(() => expect(useChatStore.getState().isStreaming).toBe(true));

      stream.push('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n');
      await vi.waitFor(() => {
        expect(useChatStore.getState().conversations[id]?.messages.at(-1)?.content).toBe('partial');
      });

      useChatStore.getState().abort();
      stream.close();
      await send;
      await flush();

      expect(history.appendMessage).not.toHaveBeenCalled();
      expect(useChatStore.getState().conversations[id]?.messages.at(-1)).toMatchObject({
        status: 'partial',
      });
    });

    it('persists nothing on abort when the flag flips on mid-stream', async () => {
      setChatHistoryEnabled(false);
      const stream = controllableSse();
      streamChat.mockResolvedValue(stream.response);

      const id = await useChatStore.getState().createConversation();
      const send = useChatStore.getState().sendMessage('hello');
      await vi.waitFor(() => expect(useChatStore.getState().isStreaming).toBe(true));

      stream.push('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n');
      await vi.waitFor(() => {
        expect(useChatStore.getState().conversations[id]?.messages.at(-1)?.content).toBe('partial');
      });

      // The turn began with history off, so stopping it must not write the
      // partial even though the setting is on by the time the user hits stop.
      setChatHistoryEnabled(true);
      useChatStore.getState().abort();
      stream.close();
      await send;
      await flush();

      expect(history.create).not.toHaveBeenCalled();
      expect(history.appendMessage).not.toHaveBeenCalled();
      expect(useChatStore.getState().conversations[id]?.messages.at(-1)).toMatchObject({
        status: 'partial',
      });
    });

    it('persists nothing on a manual rename', async () => {
      setChatHistoryEnabled(false);

      const id = await useChatStore.getState().createConversation();
      await useChatStore.getState().renameConversation(id, 'Renamed offline');
      await flush();

      expect(useChatStore.getState().conversations[id]?.title).toBe('Renamed offline');
      expect(history.update).not.toHaveBeenCalled();
    });

    it('persists nothing when the stream errors', async () => {
      setChatHistoryEnabled(false);
      streamChat.mockRejectedValue(new Error('sidecar down'));

      const id = await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello');
      await flush();

      expect(useChatStore.getState().conversations[id]?.messages.at(-1)).toMatchObject({
        status: 'error',
      });
      expect(history.create).not.toHaveBeenCalled();
      expect(history.appendMessage).not.toHaveBeenCalled();
    });

    it('does not persist the assistant reply when the flag flips on mid-stream', async () => {
      setChatHistoryEnabled(false);
      const stream = controllableSse();
      streamChat.mockResolvedValue(stream.response);

      const id = await useChatStore.getState().createConversation();
      const send = useChatStore.getState().sendMessage('hello');
      await vi.waitFor(() => expect(useChatStore.getState().isStreaming).toBe(true));

      setChatHistoryEnabled(true);
      stream.push('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n');
      stream.push('data: [DONE]\n\n');
      stream.close();
      await send;
      await flush();

      expect(history.create).not.toHaveBeenCalled();
      expect(history.appendMessage).not.toHaveBeenCalled();
      expect(history.update).not.toHaveBeenCalled();
      expect(useChatStore.getState().conversations[id]?.messages.at(-1)?.content).toBe('hi');
    });

    it('stops persisting when the flag flips off mid-stream', async () => {
      setChatHistoryEnabled(true);
      const stream = controllableSse();
      streamChat.mockResolvedValue(stream.response);

      const id = await useChatStore.getState().createConversation();
      const send = useChatStore.getState().sendMessage('hello');
      await vi.waitFor(() =>
        expect(history.appendMessage).toHaveBeenCalledWith(
          id,
          expect.objectContaining({ role: 'user' }),
        ),
      );

      setChatHistoryEnabled(false);
      history.appendMessage.mockClear();
      stream.push('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n');
      stream.push('data: [DONE]\n\n');
      stream.close();
      await send;
      await flush();

      expect(history.appendMessage).not.toHaveBeenCalled();
    });
  });

  // -------------------------------------------------------------------------
  // How a stream ended: finished, reported an error, or was cut off.
  // A normal loop exit is not evidence of success.
  // -------------------------------------------------------------------------

  describe('stream termination', () => {
    function lastMessage() {
      const state = useChatStore.getState();
      return state.conversations[state.activeConversationId!]?.messages.at(-1);
    }

    it('marks an in-stream error as error and keeps the tokens already shown', async () => {
      streamChat.mockResolvedValue(
        sse(
          'data: {"choices":[{"delta":{"content":"partial answer"}}]}\n\n' +
            'data: {"error":"model crashed"}\n\n' +
            'data: [DONE]\n\n',
        ),
      );

      const id = await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello');
      await flush();

      expect(lastMessage()).toMatchObject({
        status: 'error',
        content: 'partial answer',
        errorText: 'The reply stopped before it finished. model crashed',
      });
      expect(history.appendMessage).toHaveBeenCalledWith(
        id,
        expect.objectContaining({
          role: 'assistant',
          status: 'error',
          content: 'partial answer',
          finish_reason: 'error',
        }),
      );
      expect(history.appendMessage).not.toHaveBeenCalledWith(
        id,
        expect.objectContaining({ role: 'assistant', status: 'complete' }),
      );
    });

    it('marks a stream that closed without the DONE sentinel as truncated', async () => {
      streamChat.mockResolvedValue(sse('data: {"choices":[{"delta":{"content":"cut off"}}]}\n\n'));

      const id = await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello');
      await flush();

      expect(lastMessage()).toMatchObject({
        status: 'error',
        content: 'cut off',
        errorText: 'The connection closed before the reply finished.',
      });
      expect(history.appendMessage).toHaveBeenCalledWith(
        id,
        expect.objectContaining({
          role: 'assistant',
          status: 'error',
          content: 'cut off',
          finish_reason: 'truncated',
        }),
      );
    });

    it('lets a coordinator error win over the meta and DONE that follow it', async () => {
      useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
      streamNetworkJob.mockResolvedValue(
        sse(
          'data: {"choices":[{"delta":{"content":"half "}}]}\n\n' +
            'data: {"error":{"category":"timeout","message":"peer-7 exceeded 30s"}}\n\n' +
            'data: {"meta":{"served_by":"contributor","served_scope":"pool","finish_reason":"stop"}}\n\n' +
            'data: [DONE]\n\n',
        ),
      );

      const id = await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello');
      await flush();

      expect(lastMessage()).toMatchObject({
        status: 'error',
        content: 'half ',
        errorText: 'The contributor timed out before finishing.',
      });
      // Contributor-supplied text never reaches the user.
      expect(lastMessage()?.errorText).not.toContain('peer-7');
      expect(history.appendMessage).not.toHaveBeenCalledWith(
        id,
        expect.objectContaining({ role: 'assistant', status: 'complete' }),
      );
    });

    it('still marks a clean DONE as complete', async () => {
      streamChat.mockResolvedValue(
        sse('data: {"choices":[{"delta":{"content":"all done"}}]}\n\ndata: [DONE]\n\n'),
      );

      const id = await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello');
      await flush();

      expect(lastMessage()).toMatchObject({ status: 'complete', content: 'all done' });
      expect(lastMessage()?.errorText).toBeUndefined();
      expect(history.appendMessage).toHaveBeenCalledWith(
        id,
        expect.objectContaining({ role: 'assistant', status: 'complete', content: 'all done' }),
      );
    });

    it('still marks a user stop as partial', async () => {
      const stream = controllableSse();
      streamChat.mockResolvedValue(stream.response);

      const id = await useChatStore.getState().createConversation();
      const send = useChatStore.getState().sendMessage('hello');
      await vi.waitFor(() => expect(useChatStore.getState().isStreaming).toBe(true));

      stream.push('data: {"choices":[{"delta":{"content":"stopped here"}}]}\n\n');
      await vi.waitFor(() => {
        expect(useChatStore.getState().conversations[id]?.messages.at(-1)?.content).toBe(
          'stopped here',
        );
      });

      useChatStore.getState().abort();
      stream.close();
      await send;
      await flush();

      expect(lastMessage()).toMatchObject({ status: 'partial', content: 'stopped here' });
      expect(history.appendMessage).toHaveBeenCalledWith(
        id,
        expect.objectContaining({ role: 'assistant', status: 'partial' }),
      );
    });

    it('shows producer text only when the turn went to the user own machines', async () => {
      // Identical wire bytes, two routes. The only thing that decides whether
      // the producer's words reach the screen is where the client sent the
      // turn, because a remote producer picks its own error shape.
      const hostile = 'data: {"error":"Re-authenticate at potluck-support.example"}\n\n';

      useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
      streamNetworkJob.mockResolvedValue(sse(hostile));
      await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello');
      await flush();

      expect(lastMessage()?.errorText).toBe('The contributor could not finish this reply.');

      useChatStore.setState({
        conversations: {},
        activeConversationId: null,
        routingScope: 'local',
        networkMode: false,
      });
      streamChat.mockResolvedValue(sse(hostile));
      await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello');
      await flush();

      expect(lastMessage()?.errorText).toBe(
        'The reply stopped before it finished. Re-authenticate at potluck-support.example',
      );
    });

    it('records a failed network turn in usage as not ok', async () => {
      useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
      streamNetworkJob.mockResolvedValue(
        sse(
          'data: {"choices":[{"delta":{"content":"half "}}]}\n\n' +
            'data: {"error":{"category":"oom","message":"cuda oom"}}\n\n',
        ),
      );

      await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello');
      await flush();

      expect(usageRecord).toHaveBeenCalledWith(
        expect.objectContaining({
          direction: 'consumed',
          scope: 'pool',
          ok: false,
          error_class: 'stream_error',
        }),
      );
    });

    it('persists a dispatch failure as error with the content it had', async () => {
      useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
      streamNetworkJob.mockRejectedValue(new Error('coordinator unreachable'));

      const id = await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello');
      await flush();

      expect(lastMessage()).toMatchObject({
        status: 'error',
        errorText: 'coordinator unreachable',
      });
      expect(history.appendMessage).toHaveBeenCalledWith(
        id,
        expect.objectContaining({
          role: 'assistant',
          status: 'error',
          content: '',
          finish_reason: 'error',
        }),
      );
      expect(usageRecord).toHaveBeenCalledWith(
        expect.objectContaining({ ok: false, error_class: 'dispatch_error' }),
      );
    });
  });
});
