import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HOUSEHOLD_RECOVERY, streamHouseholdInference } from './householdInference';
import { streamTokens } from './streaming';
import type { MeshPeer } from '@/types/mesh';

const peers: MeshPeer[] = [2, 3, 4, 5].map((n) => ({
  node_id: `p${n}`, node_name: `Computer ${n}`, mesh_ip: `100.64.0.${n}`,
  wg_public_key: '', last_endpoint: null, last_seen_at: null, platform: null,
  reachable: true, reachability_mode: 'direct',
}));
const messages = [{ id: 'u1', role: 'user' as const, content: 'hello', createdAt: 1 }];
const token = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n';
const done = 'data: [DONE]\n\n';
const defaults = { ...HOUSEHOLD_RECOVERY };
const fetchMock = vi.fn();
let calls: { host: string; body: unknown }[];

function wire(body: string): Response { return new Response(body); }
function stalled(prefix = '') {
  const cancel = vi.fn();
  const response = new Response(new ReadableStream<Uint8Array>({
    start(c) { if (prefix) c.enqueue(new TextEncoder().encode(prefix)); }, cancel,
  }));
  return { response, cancel };
}
function route(responses: Record<string, Response | Error>, models: Record<string, string> = {}) {
  fetchMock.mockImplementation(async (input: string, init?: RequestInit) => {
    const url = new URL(input);
    if (url.pathname === '/peer/capabilities') {
      return Response.json({ loaded_model: models[url.hostname] ?? 'test', available_for_inference: true });
    }
    calls.push({ host: url.hostname, body: JSON.parse(String(init?.body)) });
    const response = responses[url.hostname];
    if (response instanceof Error) throw response;
    if (!response) throw new Error('Unexpected dispatch');
    return response;
  });
}
async function read(response: Response) {
  return await (async () => {
    const events = [];
    for await (const event of streamTokens(response)) events.push(event);
    return events;
  })();
}

beforeEach(() => {
  calls = [];
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  Object.assign(HOUSEHOLD_RECOVERY, { firstTokenMs: 40, idleMs: 40, totalMs: 300 });
});
afterEach(() => { vi.unstubAllGlobals(); Object.assign(HOUSEHOLD_RECOVERY, defaults); });

describe('household recovery', () => {
  it.each(['offline', 'truncated', 'error', 'unavailable'])('retries %s before output', async (kind) => {
    const failed = kind === 'offline' ? new Error('offline') : kind === 'truncated' ? wire('')
      : kind === 'error' ? wire('data: {"error":"gone"}\n\n') : new Response('', { status: 503 });
    route({ '100.64.0.2': failed, '100.64.0.3': wire(token + done) });
    const result = await streamHouseholdInference(messages, peers);
    expect(result.peer.node_id).toBe('p3');
    expect(await read(result.response)).toEqual([{ type: 'token', content: 'hi' }, { type: 'done' }]);
    expect(calls.map((c) => c.host)).toEqual(['100.64.0.2', '100.64.0.3']);
    expect(calls[0]?.body).toEqual(calls[1]?.body);
    expect(calls[0]?.body).toMatchObject({ model: 'test' });
  });

  it('times out a stalled first attempt and cancels its body', async () => {
    const first = stalled();
    route({ '100.64.0.2': first.response, '100.64.0.3': wire(token + done) });
    const result = await streamHouseholdInference(messages, peers);
    await read(result.response);
    expect(first.cancel).toHaveBeenCalledOnce();
    expect(calls).toHaveLength(2);
  });

  it('keeps partial output and does not retry a mid-answer disconnect', async () => {
    route({ '100.64.0.2': wire(token) });
    const result = await streamHouseholdInference(messages, peers);
    const events = streamTokens(result.response);
    expect((await events.next()).value).toEqual({ type: 'token', content: 'hi' });
    await expect(events.next()).rejects.toThrow('before the reply finished');
    expect(calls).toHaveLength(1);
  });

  it('bounds idle time after a token and cancels the stalled body', async () => {
    const first = stalled(token);
    route({ '100.64.0.2': first.response });
    const result = await streamHouseholdInference(messages, peers);
    await expect(read(result.response)).rejects.toThrow('stopped responding');
    expect(first.cancel).toHaveBeenCalledOnce();
    expect(calls).toHaveLength(1);
  });

  it('skips different models when retrying', async () => {
    route({ '100.64.0.2': wire(''), '100.64.0.4': wire(token + done) }, { '100.64.0.3': 'different' });
    const result = await streamHouseholdInference(messages, peers);
    await read(result.response);
    expect(calls.map((c) => c.host)).toEqual(['100.64.0.2', '100.64.0.4']);
  });

  it('does not retry after user cancellation', async () => {
    const first = stalled();
    const abort = new AbortController();
    route({ '100.64.0.2': first.response });
    const promise = streamHouseholdInference(messages, peers, { abortSignal: abort.signal });
    const check = expect(promise).rejects.toMatchObject({ name: 'AbortError' });
    await vi.waitFor(() => expect(calls).toHaveLength(1), { interval: 1 });
    abort.abort();
    await check;
    expect(first.cancel).toHaveBeenCalledOnce();
    expect(calls).toHaveLength(1);
  });

  it('accepts a valid empty completion', async () => {
    route({ '100.64.0.2': wire(done) });
    const result = await streamHouseholdInference(messages, peers);
    expect(await read(result.response)).toEqual([{ type: 'done' }]);
    expect(calls).toHaveLength(1);
  });

  it('excludes failed machines across explicit restarts', async () => {
    route({ '100.64.0.3': wire(token + done) });
    const result = await streamHouseholdInference(messages, peers, { modelSlug: 'test', excludedPeerIds: ['p2'] });
    await read(result.response);
    expect(calls.map((c) => c.host)).toEqual(['100.64.0.3']);
  });

  it('stops at the attempt limit', async () => {
    route(Object.fromEntries(peers.map((p) => [p.mesh_ip, wire('')])));
    await expect(streamHouseholdInference(messages, peers)).rejects.toThrow();
    expect(calls).toHaveLength(3);
  });

  it('stops when no compatible replacement remains', async () => {
    route({ '100.64.0.2': wire('') });
    await expect(streamHouseholdInference(messages, peers.slice(0, 1))).rejects.toThrow();
    expect(calls).toHaveLength(1);
  });
});

describe('household recovery budgets and membership', () => {
  it('does not reset the total budget between attempts', async () => {
    HOUSEHOLD_RECOVERY.totalMs = 60;
    const first = stalled();
    const second = stalled();
    route({ '100.64.0.2': first.response, '100.64.0.3': second.response });
    await expect(streamHouseholdInference(messages, peers)).rejects.toThrow();
    expect(calls).toHaveLength(2);
    expect(first.cancel).toHaveBeenCalledOnce();
    expect(second.cancel).toHaveBeenCalledOnce();
  });

  it('uses current household membership again on retry', async () => {
    route({ '100.64.0.2': wire('') });
    const roster = vi.fn().mockReturnValueOnce(peers).mockReturnValue([]);
    await expect(streamHouseholdInference(messages, peers, { getPeers: roster })).rejects.toThrow();
    expect(calls).toHaveLength(1);
    expect(roster).toHaveBeenCalledTimes(2);
  });
});
