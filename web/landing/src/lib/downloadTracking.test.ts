import { describe, expect, it, vi } from 'vitest';

import { DOWNLOADS } from './downloads';
import { isLikelyBot, logDownload, resolveDownload } from './downloadTracking';

describe('resolveDownload', () => {
  it('maps every slug to its R2 URL', () => {
    expect(resolveDownload('mac-arm')).toBe(DOWNLOADS.macArm);
    expect(resolveDownload('mac-intel')).toBe(DOWNLOADS.macIntel);
    expect(resolveDownload('windows')).toBe(DOWNLOADS.windows);
    expect(resolveDownload('linux-deb')).toBe(DOWNLOADS.linuxDeb);
    expect(resolveDownload('linux-appimage')).toBe(DOWNLOADS.linuxAppImage);
  });

  it('returns null for unknown slugs', () => {
    expect(resolveDownload('mac')).toBeNull();
    expect(resolveDownload('')).toBeNull();
    expect(resolveDownload('MAC-ARM')).toBeNull();
    expect(resolveDownload('constructor')).toBeNull();
    expect(resolveDownload('__proto__')).toBeNull();
  });
});

describe('isLikelyBot', () => {
  it('flags crawlers and link-preview fetchers', () => {
    expect(isLikelyBot('Mozilla/5.0 (compatible; Googlebot/2.1)')).toBe(true);
    expect(isLikelyBot('facebookexternalhit/1.1')).toBe(true);
    expect(isLikelyBot('Twitterbot/1.0')).toBe(true);
    expect(isLikelyBot('Mozilla/5.0 (compatible; AhrefsBot/7.0)')).toBe(true);
    expect(isLikelyBot('HeadlessChrome/120.0')).toBe(true);
  });

  it('lets real browsers and CLI downloads through', () => {
    expect(
      isLikelyBot('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'),
    ).toBe(false);
    // Linux users legitimately wget/curl the .deb onto a server.
    expect(isLikelyBot('curl/8.4.0')).toBe(false);
    expect(isLikelyBot('Wget/1.21.4')).toBe(false);
    expect(isLikelyBot(null)).toBe(false);
  });
});

describe('logDownload', () => {
  const env = { url: 'https://example.supabase.co', key: 'anon-key' };

  it('POSTs only bounded attribution and platform to the new RPC', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true });
    await logDownload('mac-arm', env, fetchImpl, { source: 'google', article: 'run-a-local-llm-on-another-computer' });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0]!;
    expect(url).toBe('https://example.supabase.co/rest/v1/rpc/potluck_log_attributed_download');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ p_platform: 'mac-arm', p_source: 'google', p_article: 'run-a-local-llm-on-another-computer' });
    expect(init.headers.apikey).toBe('anon-key');
  });

  it('never throws when the request fails', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error('network down'));
    await expect(logDownload('windows', env, fetchImpl)).resolves.toBeUndefined();
  });

  it('skips the request entirely when env is not configured', async () => {
    const fetchImpl = vi.fn();
    await logDownload('windows', { url: undefined, key: undefined }, fetchImpl);
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe('download deployment compatibility', () => {
  const env = { url: 'https://example.supabase.co', key: 'anon-key' };
  it('uses the original counter only when the new RPC is confirmed missing', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce({ status: 404, json: async () => ({ code: 'PGRST202' }) }).mockResolvedValueOnce({ ok: true });
    await logDownload('windows', env, fetchImpl);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(fetchImpl.mock.calls[1]![0]).toBe(`${env.url}/rest/v1/rpc/potluck_log_download`);
    expect(JSON.parse(fetchImpl.mock.calls[1]![1].body)).toEqual({ p_platform: 'windows' });
    expect(fetchImpl.mock.calls[0]![1].signal).toBe(fetchImpl.mock.calls[1]![1].signal);
  });
  it('does not retry uncertain failures that could double count a request', async () => {
    for (const status of [404, 500, 503]) {
      const fetchImpl = vi.fn().mockResolvedValue({ status, json: async () => ({ code: 'unexpected' }) });
      await logDownload('windows', env, fetchImpl);
      expect(fetchImpl).toHaveBeenCalledTimes(1);
    }
  });
});
