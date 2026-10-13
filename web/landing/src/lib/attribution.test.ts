import { describe, expect, it } from 'vitest';
import { articleAtPath, classifySource, nextAttribution, redactAnalyticsUrl, VISIT_TTL_MS } from './attribution';

const published = ['run-a-local-llm-on-another-computer'];
const article = new URL(`https://trypotluck.ai/blog/${published[0]}`);
const install = new URL('https://trypotluck.ai/install');

describe('source attribution', () => {
  it('classifies search, social and direct without keeping a raw referrer', () => {
    expect(classifySource(article, 'https://www.google.com/search?q=private')).toBe('google');
    expect(classifySource(article, 'https://old.reddit.com/r/test')).toBe('reddit');
    expect(classifySource(article, 'https://google.com.evil.example/private')).toBe('other-referral');
    expect(classifySource(article, 'https://trypotluck.ai/')).toBe('direct');
    expect(classifySource(article, '')).toBe('direct');
  });
  it('distinguishes marked paid visits and discards arbitrary campaign values', () => {
    expect(classifySource(new URL(`${article}?utm_source=google&utm_medium=cpc&gclid=secret`), '')).toBe('paid-campaign');
    expect(classifySource(new URL(`${article}?utm_source=person@example.com`), '')).toBe('campaign');
    expect(classifySource(new URL(`${article}?utm_source=__proto__`), '')).toBe('campaign');
    expect(classifySource(new URL(`${article}?utm_medium=email`), '')).toBe('email');
  });
  it('preserves the entry source and last published article on the path to installation', () => {
    const first = nextAttribution(null, article, 'https://www.google.com/search?q=private', published, 1000);
    const next = nextAttribution(JSON.stringify(first), install, article.href, published, 2000);
    expect(next).toEqual({ source: 'google', article: published[0], expires: 1000 + VISIT_TTL_MS });
    expect(JSON.stringify(next)).not.toContain('private');
  });
  it('expires visits and ignores corrupted or unrecognized storage', () => {
    expect(nextAttribution('{broken', install, '', published, 1000).source).toBe('direct');
    expect(nextAttribution(JSON.stringify({ source: 'email@example.com', article: 'private', expires: 2000 }), install, '', published, 1000).source).toBe('direct');
    const old = { source: 'google', article: published[0], expires: 1000 };
    expect(nextAttribution(JSON.stringify(old), install, '', published, 1001)).toEqual({ source: 'direct', article: null, expires: 1001 + VISIT_TTL_MS });
  });
  it('accepts only published article paths and drops arbitrary URL data', () => {
    expect(articleAtPath('/blog/private-email-example-com', published)).toBeNull();
    expect(articleAtPath(`/blog/${published[0]}/`, published)).toBe(published[0]);
    expect(redactAnalyticsUrl(`${article}?email=secret@example.com#token`)).toBe(article.href);
    expect(redactAnalyticsUrl('https://trypotluck.ai/auth/verify?token=secret')).toBeNull();
  });
});


it('drops raw campaign values and unpublished article slugs at the server boundary', async () => {
  const { downloadAttribution } = await import('./attribution');
  expect(downloadAttribution(new URL('https://trypotluck.ai/dl/windows?source=private-term&article=draft'), ['published'])).toEqual({ source: 'unknown', article: null });
  expect(downloadAttribution(new URL('https://trypotluck.ai/dl/windows?source=google&article=published'), ['published'])).toEqual({ source: 'google', article: 'published' });
});
