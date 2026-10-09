import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/stores/connectionStore', () => ({
  coordinatorBase: () => 'http://coordinator.test',
}));
vi.mock('@/lib/coordinatorAuth', () => ({
  coordinatorSessionRequest: vi.fn(),
  detailOf: vi.fn(),
}));

const fetchMock = vi.fn();
let localApi: typeof import('./localApi')['localApi'];

beforeAll(async () => {
  // localApi captures fetch at import time, just as it does in the app.
  vi.stubGlobal('fetch', fetchMock);
  ({ localApi } = await import('./localApi'));
});
beforeEach(() => fetchMock.mockReset().mockResolvedValue(new Response('')));
afterAll(() => vi.unstubAllGlobals());

const CIRCLE_ID = '11111111-1111-4111-8111-111111111111';
const ENGINE_URL = 'http://127.0.0.1:8321/chat/circle';
const COORDINATOR_URL = 'http://coordinator.test/jobs/stream';

describe('network request routing contract', () => {
  it('sends an explicit circle scope with the selected circle ID', async () => {
    const controller = new AbortController();
    await localApi.streamNetworkJob('private circle prompt', {
      modelSlug: 'test-model',
      circleId: CIRCLE_ID,
      abortSignal: controller.signal,
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    // A circle chat goes to the local engine first, which links directly or relays.
    expect(url).toBe(ENGINE_URL);
    expect(init.signal).toBe(controller.signal);
    expect(JSON.parse(String(init.body))).toEqual({
      prompt: 'private circle prompt',
      model_slug: 'test-model',
      sampling: { temperature: 0.7, max_tokens: 2048 },
      scope: 'circle',
      circle_id: CIRCLE_ID,
    });
  });

  it('sends the conversation to a circle as role and content only', async () => {
    await localApi.streamNetworkJob('and Italy?', {
      modelSlug: 'test-model',
      circleId: CIRCLE_ID,
      messages: [
        { role: 'user', content: 'capital of France?', id: 'm1', createdAt: 5 } as never,
        { role: 'assistant', content: 'Paris.' },
        { role: 'user', content: 'and Italy?' },
      ],
    });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body));
    expect(body.prompt).toBe('and Italy?');
    expect(body.messages).toEqual([
      { role: 'user', content: 'capital of France?' },
      { role: 'assistant', content: 'Paris.' },
      { role: 'user', content: 'and Italy?' },
    ]);
  });

  it('never sends history to the open pool, even when handed some', async () => {
    await localApi.streamNetworkJob('pool prompt', {
      modelSlug: 'test-model',
      messages: [{ role: 'user', content: 'earlier' }, { role: 'user', content: 'pool prompt' }],
    });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).not.toHaveProperty('messages');
  });

  it('sends an explicit pool scope without a circle ID for open-pool chat', async () => {
    await localApi.streamNetworkJob('pool prompt', { modelSlug: 'test-model' });
    // Open-pool chat is untouched: one request, to the coordinator, never the engine.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(COORDINATOR_URL);
    const body = JSON.parse(String(init.body));
    expect(body.scope).toBe('pool');
    expect(body).not.toHaveProperty('circle_id');
    expect(body).not.toHaveProperty('messages');
  });
});

describe('circle chat through the engine', () => {
  const resync = vi.fn<() => Promise<boolean>>();

  function jsonResponse(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  }
  const urls = () => fetchMock.mock.calls.map(([url]) => url as string);
  const bodies = () => fetchMock.mock.calls.map(([, init]) => String((init as RequestInit).body));
  const circleChat = (abortSignal?: AbortSignal) =>
    localApi.streamNetworkJob('private circle prompt', {
      modelSlug: 'test-model',
      circleId: CIRCLE_ID,
      messages: [{ role: 'user', content: 'private circle prompt' }],
      ...(abortSignal ? { abortSignal } : {}),
    });
  /** A request that stays in flight until the caller aborts it, as fetch does. */
  const hangsUntilAborted = (_url: string, init: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
    });
  function installBridge(): void {
    (window as unknown as { potluck?: unknown }).potluck = { cloud: { resyncEngineSession: resync } };
  }

  beforeEach(() => {
    resync.mockReset().mockResolvedValue(true);
    installBridge();
  });
  afterEach(() => {
    delete (window as unknown as { potluck?: unknown }).potluck;
    // Whatever path a test took, no request of its carried any other scope.
    for (const body of bodies()) {
      expect(JSON.parse(body)).toMatchObject({ scope: 'circle', circle_id: CIRCLE_ID });
    }
  });

  it('calls the engine the way streamChat does, with no credentials of its own', async () => {
    await circleChat();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
  });

  it('returns the engine response untouched and makes no other request', async () => {
    const stream = new Response('data: [DONE]\n\n');
    fetchMock.mockResolvedValueOnce(stream);
    expect(await circleChat()).toBe(stream);
    expect(urls()).toEqual([ENGINE_URL]);
    expect(resync).not.toHaveBeenCalled();
  });

  describe('when the engine cannot take the request', () => {
    it('falls back to the coordinator with the identical body on a bare 404', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(404, { detail: 'Not Found' }));
      const coordinator = new Response('data: [DONE]\n\n');
      fetchMock.mockResolvedValueOnce(coordinator);

      expect(await circleChat()).toBe(coordinator);
      expect(urls()).toEqual([ENGINE_URL, COORDINATOR_URL]);
      expect(bodies()[1]).toBe(bodies()[0]);
      expect(resync).not.toHaveBeenCalled();
    });

    it('falls back on a 405', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(405, { detail: 'Method Not Allowed' }));
      await circleChat();
      expect(urls()).toEqual([ENGINE_URL, COORDINATOR_URL]);
      expect(bodies()[1]).toBe(bodies()[0]);
    });

    it('falls back when the engine is not running', async () => {
      fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
      await circleChat();
      expect(urls()).toEqual([ENGINE_URL, COORDINATOR_URL]);
      expect(bodies()[1]).toBe(bodies()[0]);
    });

    it('treats a 404 with no JSON detail as a missing route', async () => {
      fetchMock.mockResolvedValueOnce(new Response('<html>nope</html>', { status: 404 }));
      await circleChat();
      expect(urls()).toEqual([ENGINE_URL, COORDINATOR_URL]);
    });

    it('sends the coordinator the same signal as the engine', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(404, { detail: 'Not Found' }));
      const controller = new AbortController();
      await circleChat(controller.signal);
      const signals = fetchMock.mock.calls.map(([, init]) => (init as RequestInit).signal);
      expect(signals).toEqual([controller.signal, controller.signal]);
    });
  });

  describe('when the coordinator has already answered through the engine', () => {
    it('passes a 404 with the coordinator\'s own detail through, body intact, with no second request', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(404, { detail: 'circle not found' }));
      const res = await circleChat();
      expect(res.status).toBe(404);
      expect(await res.json()).toEqual({ detail: 'circle not found' });
      expect(urls()).toEqual([ENGINE_URL]);
    });

    it('passes a 404 whose detail is an object through', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(404, { detail: { code: 'circle_gone' } }));
      const res = await circleChat();
      expect(res.status).toBe(404);
      expect(await res.json()).toEqual({ detail: { code: 'circle_gone' } });
      expect(urls()).toEqual([ENGINE_URL]);
    });

    it.each([
      [403, { detail: 'Only the owner can do that.' }],
      [422, { detail: [{ type: 'uuid_parsing', loc: ['body', 'circle_id'] }] }],
      [429, { detail: 'Too many requests.' }],
      [503, { detail: "No online peer serves model 'test-model'" }],
      [500, { detail: 'Internal Server Error' }],
    ])('passes a %i through as it is, with no second request', async (status, body) => {
      fetchMock.mockResolvedValueOnce(jsonResponse(status, body));
      const res = await circleChat();
      expect(res.status).toBe(status);
      expect(await res.json()).toEqual(body);
      expect(urls()).toEqual([ENGINE_URL]);
      expect(resync).not.toHaveBeenCalled();
    });
  });

  describe('when the engine answers 401', () => {
    it('asks main to hand the session over again, once, and retries the engine once', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: 'Sign in to use a trusted circle.' }));
      const stream = new Response('data: [DONE]\n\n');
      fetchMock.mockResolvedValueOnce(stream);

      expect(await circleChat()).toBe(stream);
      expect(resync).toHaveBeenCalledTimes(1);
      expect(urls()).toEqual([ENGINE_URL, ENGINE_URL]);
      expect(bodies()[1]).toBe(bodies()[0]);
    });

    it('falls back to the coordinator, with the same body, when the retry is a 401 too', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }));
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }));
      const coordinator = new Response('data: [DONE]\n\n');
      fetchMock.mockResolvedValueOnce(coordinator);

      expect(await circleChat()).toBe(coordinator);
      expect(resync).toHaveBeenCalledTimes(1);
      expect(urls()).toEqual([ENGINE_URL, ENGINE_URL, COORDINATOR_URL]);
      expect(new Set(bodies()).size).toBe(1);
    });

    it('does not retry when main had no session to hand over', async () => {
      resync.mockResolvedValue(false);
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: 'Sign in to use a trusted circle.' }));
      await circleChat();
      expect(resync).toHaveBeenCalledTimes(1);
      expect(urls()).toEqual([ENGINE_URL, COORDINATOR_URL]);
    });

    it('does not retry when the hand-off fails', async () => {
      resync.mockRejectedValue(new Error('ipc closed'));
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }));
      await circleChat();
      expect(urls()).toEqual([ENGINE_URL, COORDINATOR_URL]);
    });

    it.each([
      ['there is no Electron bridge', () => delete (window as unknown as { potluck?: unknown }).potluck],
      ['an older preload has no resync call', () => {
        (window as unknown as { potluck?: unknown }).potluck = { cloud: {} };
      }],
    ])('falls back straight away when %s', async (_name, setUp) => {
      setUp();
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }));
      await circleChat();
      expect(resync).not.toHaveBeenCalled();
      expect(urls()).toEqual([ENGINE_URL, COORDINATOR_URL]);
    });

    it('falls back when the engine route turns out to be missing on the retry', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }));
      fetchMock.mockResolvedValueOnce(jsonResponse(404, { detail: 'Not Found' }));
      await circleChat();
      expect(urls()).toEqual([ENGINE_URL, ENGINE_URL, COORDINATOR_URL]);
    });

    it('passes the coordinator\'s own refusal on the retry through', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }));
      fetchMock.mockResolvedValueOnce(jsonResponse(503, { detail: 'No online peer' }));
      const res = await circleChat();
      expect(res.status).toBe(503);
      expect(urls()).toEqual([ENGINE_URL, ENGINE_URL]);
    });

    it('never makes more than three requests or one hand-off', async () => {
      fetchMock.mockImplementation(async (url: string) =>
        url === COORDINATOR_URL ? new Response('data: [DONE]\n\n') : jsonResponse(401, { detail: 'expired' }));
      await circleChat();
      expect(fetchMock).toHaveBeenCalledTimes(3);
      expect(resync).toHaveBeenCalledTimes(1);
    });
  });

  describe('when the caller aborts', () => {
    it('cancels the request to the engine and does not fall back', async () => {
      fetchMock.mockImplementationOnce(hangsUntilAborted);
      const controller = new AbortController();
      const pending = circleChat(controller.signal);
      controller.abort();
      await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
      expect(urls()).toEqual([ENGINE_URL]);
    });

    it('stops while main is handing the session over, before any retry', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }));
      let finishHandOff: (handedOver: boolean) => void = () => undefined;
      resync.mockReturnValue(new Promise<boolean>((resolve) => { finishHandOff = resolve; }));
      const controller = new AbortController();
      const pending = circleChat(controller.signal);
      await vi.waitFor(() => expect(resync).toHaveBeenCalled());
      controller.abort();
      finishHandOff(true);
      await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
      expect(urls()).toEqual([ENGINE_URL]);
    });

    it('cancels the retry to the engine and does not fall back', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }));
      fetchMock.mockImplementationOnce(hangsUntilAborted);
      const controller = new AbortController();
      const pending = circleChat(controller.signal);
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
      controller.abort();
      await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
      expect(urls()).toEqual([ENGINE_URL, ENGINE_URL]);
    });

    it('cancels the coordinator fallback', async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(404, { detail: 'Not Found' }));
      fetchMock.mockImplementationOnce(hangsUntilAborted);
      const controller = new AbortController();
      const pending = circleChat(controller.signal);
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
      controller.abort();
      await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
      expect(urls()).toEqual([ENGINE_URL, COORDINATOR_URL]);
    });

    it('does not fall back when the caller stopped while the engine was failing', async () => {
      fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
      const controller = new AbortController();
      const pending = circleChat(controller.signal);
      controller.abort();
      await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
      expect(urls()).toEqual([ENGINE_URL]);
    });
  });
});
