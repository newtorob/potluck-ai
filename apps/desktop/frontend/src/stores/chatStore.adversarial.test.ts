/**
 * Adversarial review of the stream-termination fix.
 *
 * Adversaries, in order of how much they can reach:
 *   - an open-pool contributor: controls the `message` field of a coordinator
 *     error envelope and the reply text itself. It must never put its own
 *     words on the user's screen and must never produce a row that reads as a
 *     successful completion.
 *   - the coordinator: controls the whole SSE envelope, including whether a
 *     `category` is present and what it says.
 *   - the transport: can cut the body anywhere, with or without [DONE].
 *
 * Mock setup is lifted from chatStore.test.ts, plus a householdInference mock
 * so the my-machines dispatch path is reachable.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

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
const streamHousehold = vi.hoisted(() => vi.fn());

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
    getState: () => ({ peers: [{ node_name: 'studio-mac', mesh_ip: '100.64.0.2' }] }),
  },
}));

vi.mock('@/lib/householdInference', () => ({
  streamHouseholdInference: streamHousehold,
}));

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

import { useChatStore } from './chatStore';
import type { AppMessage } from './chatStore';
import { MessageBubble } from '@/components/chat/MessageBubble';

const SSE_HEADERS = { 'Content-Type': 'text/event-stream' };

function doneSse(): Response {
  return new Response('data: [DONE]\n\n', { status: 200, headers: SSE_HEADERS });
}

function sse(body: string): Response {
  return new Response(body, { status: 200, headers: SSE_HEADERS });
}

/** A body that delivers `prefix`, then errors the stream: a transport drop
 *  mid-reply, which surfaces as a rejected reader.read(). */
function erroringSse(prefix: string, err: Error): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(encoder.encode(prefix));
      setTimeout(() => c.error(err), 0);
    },
  });
  return new Response(body, { status: 200, headers: SSE_HEADERS });
}

function controllableSse(): { response: Response; push: (chunk: string) => void; close: () => void } {
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      ctrl = c;
    },
  });
  return {
    response: new Response(body, { status: 200, headers: SSE_HEADERS }),
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

async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function lastMessage(): AppMessage | undefined {
  const state = useChatStore.getState();
  return state.conversations[state.activeConversationId!]?.messages.at(-1);
}

function assistantAppends() {
  return history.appendMessage.mock.calls.filter(
    (c) => (c[1] as { role?: string } | undefined)?.role === 'assistant',
  );
}

function renderBubble(msg: AppMessage) {
  return render(createElement(MemoryRouter, null, createElement(MessageBubble, { message: msg })));
}

function message(overrides: Partial<AppMessage> = {}): AppMessage {
  return {
    id: 'm1',
    role: 'assistant',
    content: '',
    createdAt: Date.now(),
    status: 'complete',
    ...overrides,
  };
}

/** Text a hostile peer would love to get in front of the user. */
const HOSTILE = 'Potluck session expired. Re-authenticate at potluck-support.example';

beforeEach(() => {
  history.create.mockReset().mockResolvedValue({});
  history.appendMessage.mockReset().mockResolvedValue({});
  history.update.mockReset().mockResolvedValue({});
  history.list.mockReset().mockResolvedValue([]);
  history.delete.mockReset().mockResolvedValue(undefined);
  streamChat.mockReset().mockResolvedValue(doneSse());
  streamNetworkJob.mockReset().mockResolvedValue(doneSse());
  streamHousehold.mockReset();
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

// ---------------------------------------------------------------------------
// Contributor-supplied text must not reach the screen on a network route
// ---------------------------------------------------------------------------

describe('contributor text containment', () => {
  it('DEFECT 2: an error object with no category must not render peer text', async () => {
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    streamNetworkJob.mockResolvedValue(
      sse(
        'data: {"choices":[{"delta":{"content":"half "}}]}\n\n' +
          `data: ${JSON.stringify({ error: { message: HOSTILE } })}\n\n`,
      ),
    );

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(lastMessage()?.status).toBe('error');
    expect(String(lastMessage()?.errorText)).not.toContain('potluck-support.example');
  });

  it('DEFECT 2b: a non-string category must not downgrade to the trusted branch', async () => {
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    streamNetworkJob.mockResolvedValue(
      sse(`data: ${JSON.stringify({ error: { category: 7, message: HOSTILE } })}\n\n`),
    );

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(lastMessage()?.status).toBe('error');
    expect(String(lastMessage()?.errorText)).not.toContain('potluck-support.example');
  });

  it('a string-shaped error on the pool path never renders producer text', async () => {
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    streamNetworkJob.mockResolvedValue(sse(`data: ${JSON.stringify({ error: HOSTILE })}\n\n`));

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    // The trust decision is keyed on the turn's routing scope, which the client
    // knows for certain, not on the shape of the error the producer chose.
    expect(lastMessage()?.errorText).not.toContain('potluck-support.example');
    expect(lastMessage()?.errorText).toBe('The contributor could not finish this reply.');
  });

  it('documents that a non-ok coordinator response body is rendered verbatim', async () => {
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    streamNetworkJob.mockResolvedValue(
      new Response(JSON.stringify({ detail: HOSTILE }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(lastMessage()?.errorText).toContain('potluck-support.example');
  });

  it('maps a known category to fixed copy and drops the peer message', async () => {
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    streamNetworkJob.mockResolvedValue(
      sse(`data: ${JSON.stringify({ error: { category: 'declined', message: HOSTILE } })}\n\n`),
    );

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(lastMessage()?.errorText).toBe('The contributor declined this request.');
  });

  it('maps an unknown string category to the fallback and drops the peer message', async () => {
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    streamNetworkJob.mockResolvedValue(
      sse(`data: ${JSON.stringify({ error: { category: 'quantum_flux', message: HOSTILE } })}\n\n`),
    );

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(lastMessage()?.errorText).toBe('The contributor could not finish this reply.');
  });
});

// ---------------------------------------------------------------------------
// Prototype keys in the category lookup
// ---------------------------------------------------------------------------

describe('prototype-key categories', () => {
  it('DEFECT 3: a prototype-key category must still produce a string', async () => {
    for (const key of ['__proto__', 'constructor', 'toString']) {
      useChatStore.setState({
        conversations: {},
        activeConversationId: null,
        routingScope: 'cooperative',
        networkMode: true,
      });
      streamNetworkJob.mockResolvedValue(
        sse(`data: ${JSON.stringify({ error: { category: key, message: 'm' } })}\n\n`),
      );

      await useChatStore.getState().createConversation();
      await useChatStore.getState().sendMessage('hello');
      await flush();

      expect(typeof lastMessage()?.errorText, `category ${key}`).toBe('string');
      expect(lastMessage()?.errorText, `category ${key}`).toBe(
        'The contributor could not finish this reply.',
      );
    }
  });

  it('DEFECT 3b: the bubble must render that message without throwing', async () => {
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    streamNetworkJob.mockResolvedValue(
      sse(`data: ${JSON.stringify({ error: { category: '__proto__', message: 'm' } })}\n\n`),
    );

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    const msg = lastMessage()!;
    expect(() => renderBubble(msg)).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// Terminal decision under adversarial framing
// ---------------------------------------------------------------------------

describe('terminal decision', () => {
  it('keeps the privacy gate closed on a failed stream with history off', async () => {
    setChatHistoryEnabled(false);
    streamChat.mockResolvedValue(
      sse(
        'data: {"choices":[{"delta":{"content":"partial answer"}}]}\n\n' +
          'data: {"error":"model crashed"}\n\ndata: [DONE]\n\n',
      ),
    );

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(lastMessage()).toMatchObject({ status: 'error', content: 'partial answer' });
    expect(history.create).not.toHaveBeenCalled();
    expect(history.appendMessage).not.toHaveBeenCalled();
  });

  it('marks a stream that closed before any token as truncated, not complete', async () => {
    streamChat.mockResolvedValue(sse(''));

    const id = await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(lastMessage()).toMatchObject({
      status: 'error',
      content: '',
      errorText: 'The connection closed before the reply finished.',
    });
    expect(history.appendMessage).toHaveBeenCalledWith(
      id,
      expect.objectContaining({ role: 'assistant', status: 'error', finish_reason: 'truncated' }),
    );
  });

  it('drops content that arrives after an error envelope', async () => {
    streamChat.mockResolvedValue(
      sse(
        'data: {"choices":[{"delta":{"content":"before"}}]}\n\n' +
          'data: {"error":"peer died"}\n\n' +
          'data: {"choices":[{"delta":{"content":" AFTER"}}]}\n\n' +
          'data: [DONE]\n\n',
      ),
    );

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(lastMessage()?.content).toBe('before');
    expect(lastMessage()?.status).toBe('error');
    expect(assistantAppends()).toHaveLength(1);
    expect(assistantAppends()[0]?.[1]).toMatchObject({ status: 'error', content: 'before' });
  });

  it('ends a mid-stream transport drop as error with the content preserved', async () => {
    useChatStore.setState({ routingScope: 'household', householdMode: true });
    streamHousehold.mockResolvedValue({
      response: erroringSse(
        'data: {"choices":[{"delta":{"content":"half an answer"}}]}\n\n',
        new Error('mesh connection reset'),
      ),
      peer: { node_name: 'studio-mac' },
      capabilities: {},
    });

    const id = await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(lastMessage()).toMatchObject({ status: 'error', content: 'half an answer' });
    expect(useChatStore.getState().isStreaming).toBe(false);
    expect(history.appendMessage).toHaveBeenCalledWith(
      id,
      expect.objectContaining({ role: 'assistant', status: 'error', content: 'half an answer' }),
    );
    expect(history.appendMessage).not.toHaveBeenCalledWith(
      id,
      expect.objectContaining({ role: 'assistant', status: 'complete' }),
    );
    // Observation, not a bug: a drop that happened after tokens were already
    // read is filed under the same class as a dispatch that never opened.
    expect(usageRecord).toHaveBeenCalledWith(
      expect.objectContaining({ ok: false, error_class: 'dispatch_error' }),
    );
  });

  it('leaves exactly one terminal status when abort races an error envelope', async () => {
    const stream = controllableSse();
    streamChat.mockResolvedValue(stream.response);

    const id = await useChatStore.getState().createConversation();
    const send = useChatStore.getState().sendMessage('hello');
    await vi.waitFor(() => expect(useChatStore.getState().isStreaming).toBe(true));

    stream.push('data: {"choices":[{"delta":{"content":"raced"}}]}\n\n');
    await vi.waitFor(() => {
      expect(useChatStore.getState().conversations[id]?.messages.at(-1)?.content).toBe('raced');
    });

    // The error lands and the user hits stop in the same tick.
    stream.push('data: {"error":"peer died"}\n\n');
    useChatStore.getState().abort();
    stream.close();
    await send;
    await flush();

    // Stop wins: the abort check at the top of the loop fires before the error
    // event is consumed, so the user's own action is what the row records.
    const final = lastMessage();
    expect(final?.status).toBe('partial');
    expect(final?.content).toBe('raced');
    expect(useChatStore.getState().isStreaming).toBe(false);
    expect(useChatStore.getState().streamingMessageId).toBeNull();
    // One terminal write only: no double persist of the same assistant turn.
    expect(assistantAppends()).toHaveLength(1);
    expect(assistantAppends()[0]?.[1]).not.toMatchObject({ status: 'complete' });
  });

  it('does not turn a meta envelope seen before the error into a success', async () => {
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    streamNetworkJob.mockResolvedValue(
      sse(
        'data: {"choices":[{"delta":{"content":"half "}}]}\n\n' +
          'data: {"meta":{"served_by":"contributor","served_scope":"pool","output_tokens":4,"finish_reason":"stop"}}\n\n' +
          'data: {"error":{"category":"peer_disconnected","message":"gone"}}\n\n' +
          'data: [DONE]\n\n',
      ),
    );

    const id = await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(lastMessage()?.status).toBe('error');
    expect(history.appendMessage).toHaveBeenCalledWith(
      id,
      expect.objectContaining({
        role: 'assistant',
        status: 'error',
        finish_reason: 'error',
        served_by: 'contributor',
      }),
    );
    expect(usageRecord).toHaveBeenCalledWith(
      expect.objectContaining({ ok: false, error_class: 'stream_error' }),
    );
  });
});

// ---------------------------------------------------------------------------
// Usage telemetry per routing scope
// ---------------------------------------------------------------------------

describe('usage telemetry on failure', () => {
  it('records a household failure as my_machines', async () => {
    useChatStore.setState({ routingScope: 'household', householdMode: true });
    streamHousehold.mockResolvedValue({
      response: sse('data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: {"error":"peer died"}\n\n'),
      peer: { node_name: 'studio-mac' },
      capabilities: {},
    });

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(usageRecord).toHaveBeenCalledWith(
      expect.objectContaining({ scope: 'my_machines', ok: false, error_class: 'stream_error' }),
    );
  });

  it('records a circle failure as circle', async () => {
    useChatStore.setState({ routingScope: 'circle', activeCircleId: 'circle-1' });
    streamNetworkJob.mockResolvedValue(
      sse('data: {"error":{"category":"oom","message":"m"}}\n\n'),
    );

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(usageRecord).toHaveBeenCalledWith(
      expect.objectContaining({ scope: 'circle', ok: false, error_class: 'stream_error' }),
    );
  });

  it('records nothing for a local failure', async () => {
    streamChat.mockResolvedValue(sse('data: {"error":"model crashed"}\n\ndata: [DONE]\n\n'));

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(lastMessage()?.status).toBe('error');
    expect(usageRecord).not.toHaveBeenCalled();
  });

  it('records a truncated network turn with the truncated error class', async () => {
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    streamNetworkJob.mockResolvedValue(sse('data: {"choices":[{"delta":{"content":"cut"}}]}\n\n'));

    await useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('hello');
    await flush();

    expect(usageRecord).toHaveBeenCalledWith(
      expect.objectContaining({ ok: false, error_class: 'truncated' }),
    );
  });
});

// ---------------------------------------------------------------------------
// What the failed bubble actually shows
// ---------------------------------------------------------------------------

describe('failed message rendering', () => {
  it('renders an errorText containing HTML as inert text', () => {
    renderBubble(
      message({ status: 'error', content: 'partial', errorText: '<img src=x onerror="alert(1)">' }),
    );

    expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeTruthy();
    expect(document.querySelector('img')).toBeNull();
  });

  it('renders hostile markup inside the preserved partial answer as inert text', () => {
    renderBubble(
      message({
        status: 'error',
        content: '<img src=x onerror="alert(1)"> [click](javascript:alert(2))',
        errorText: 'The contributor timed out before finishing.',
      }),
    );

    expect(document.querySelector('img')).toBeNull();
    expect(document.querySelector('a')).toBeNull();
  });

  it('shows no route receipt on an errored message that carries meta', () => {
    renderBubble(
      message({
        status: 'error',
        content: 'half an answer',
        errorText: 'The contributor timed out before finishing.',
        meta: { served_by: 'contributor', served_scope: 'pool', output_tokens: 4 },
      }),
    );

    expect(screen.queryByText('Open pool')).toBeNull();
    expect(screen.queryByText('This machine')).toBeNull();
    expect(screen.getByText('half an answer')).toBeTruthy();
  });

  it('shows no stopped-early marker on an errored message', () => {
    renderBubble(message({ status: 'error', content: 'half', errorText: 'x' }));

    expect(screen.queryByText('Stopped early')).toBeNull();
  });
});
