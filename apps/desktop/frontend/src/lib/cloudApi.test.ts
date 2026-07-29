import { describe, it, expect, vi, afterEach } from 'vitest';
import { cloudApi, CloudApiError } from '@/lib/cloudApi';

/** Stub global fetch with a single canned Response-like object. */
function mockFetch(response: { ok: boolean; status?: number; json: () => Promise<unknown> }) {
  const fn = vi.fn().mockResolvedValue({ statusText: '', ...response });
  vi.stubGlobal('fetch', fn);
  return fn;
}

describe('cloudApi billing', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('createCheckout POSTs the plan with credentials and returns the url', async () => {
    const fetchMock = mockFetch({ ok: true, json: async () => ({ url: 'https://stripe/checkout' }) });
    const res = await cloudApi.createCheckout('founding');

    expect(res).toEqual({ url: 'https://stripe/checkout' });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/billing\/checkout$/);
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('include');
    expect(JSON.parse(String(init.body))).toEqual({ plan: 'founding' });
  });

  it('getBillingPortal POSTs to /billing/portal', async () => {
    const fetchMock = mockFetch({ ok: true, json: async () => ({ url: 'https://stripe/portal' }) });
    const res = await cloudApi.getBillingPortal();

    expect(res.url).toBe('https://stripe/portal');
    expect(String((fetchMock.mock.calls[0] as [string])[0])).toMatch(/\/billing\/portal$/);
  });

  it('surfaces the server error detail on a non-ok response', async () => {
    mockFetch({ ok: false, status: 503, json: async () => ({ detail: 'Billing is not configured' }) });
    await expect(cloudApi.createCheckout('founding')).rejects.toThrow(/503: Billing is not configured/);
  });
});

describe('cloudApi auth failure classification', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('getMe() rejects with a CloudApiError carrying the HTTP status on a 401', async () => {
    mockFetch({ ok: false, status: 401, json: async () => ({ detail: 'Not authenticated' }) });
    const err = await cloudApi.getMe().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(CloudApiError);
    expect((err as CloudApiError).status).toBe(401);
  });

  it('getMe() rejects with status 0 on a network failure (fetch itself rejects)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const err = await cloudApi.getMe().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(CloudApiError);
    expect((err as CloudApiError).status).toBe(0);
  });

  it('hasToken() resolves null outside the packaged app (no Electron bridge)', async () => {
    await expect(cloudApi.hasToken()).resolves.toBeNull();
  });

  it('clearToken() is a no-op outside the packaged app (no Electron bridge)', async () => {
    await expect(cloudApi.clearToken()).resolves.toBeUndefined();
  });
});

describe('cloudApi admin', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('adminListMembers builds the paginated query string', async () => {
    const fetchMock = mockFetch({
      ok: true,
      json: async () => ({ total: 0, limit: 10, offset: 5, members: [] }),
    });
    await cloudApi.adminListMembers(10, 5);
    expect(String((fetchMock.mock.calls[0] as [string])[0])).toMatch(/\/admin\/members\?limit=10&offset=5$/);
  });

  it('adminRevokeFounding issues a DELETE', async () => {
    const fetchMock = mockFetch({ ok: true, json: async () => ({ revoked: true, member: {} }) });
    await cloudApi.adminRevokeFounding('abc-123');
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toMatch(/\/admin\/members\/abc-123\/founding$/);
    expect(init.method).toBe('DELETE');
  });
});
