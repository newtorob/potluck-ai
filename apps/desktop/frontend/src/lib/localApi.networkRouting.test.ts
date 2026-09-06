import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

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

describe('network request routing contract', () => {
  it('sends an explicit circle scope with the selected circle ID', async () => {
    const circleId = '11111111-1111-4111-8111-111111111111';
    const controller = new AbortController();
    await localApi.streamNetworkJob('private circle prompt', {
      modelSlug: 'test-model',
      circleId,
      abortSignal: controller.signal,
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://coordinator.test/jobs/stream');
    expect(init.signal).toBe(controller.signal);
    expect(JSON.parse(String(init.body))).toEqual({
      prompt: 'private circle prompt',
      model_slug: 'test-model',
      sampling: { temperature: 0.7, max_tokens: 2048 },
      scope: 'circle',
      circle_id: circleId,
    });
  });

  it('sends an explicit pool scope without a circle ID for open-pool chat', async () => {
    await localApi.streamNetworkJob('pool prompt', { modelSlug: 'test-model' });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body));
    expect(body.scope).toBe('pool');
    expect(body).not.toHaveProperty('circle_id');
    expect(body).not.toHaveProperty('messages');
  });
});
