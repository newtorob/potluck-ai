// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SiteAnalytics } from './SiteAnalytics';
import { ATTRIBUTION_KEY } from '@/lib/attribution';

vi.mock('@vercel/analytics/react', () => ({ Analytics: () => null }));
vi.mock('next/navigation', () => ({ usePathname: () => window.location.pathname }));
const publishedArticles = ['run-a-local-llm-on-another-computer'];
let host: HTMLDivElement;
let root: Root;

async function render(path: string) {
  window.history.replaceState(null, '', path);
  await act(async () => root.render(createElement(SiteAnalytics, { publishedArticles })));
}

function clickLink(href: string, button = 0) {
  const anchor = document.createElement('a'); anchor.href = href;
  const inner = document.createElement('span'); anchor.append(inner); host.append(anchor);
  // Stop JSDOM navigation after the attribution handler, preserving event order.
  const stopNavigation = (event: Event) => event.preventDefault();
  const type = button === 1 ? 'auxclick' : 'click';
  document.addEventListener(type, stopNavigation);
  inner.dispatchEvent(new MouseEvent(type, { button, bubbles: true, cancelable: true }));
  document.removeEventListener(type, stopNavigation);
  return anchor;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  sessionStorage.clear();
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); });

it('attributes a nested-icon download click after article-to-install navigation', async () => {
  await render(`/blog/${publishedArticles[0]}?utm_source=google`);
  await render('/install');
  const link = clickLink('/dl/windows');
  expect(link.getAttribute('href')).toBe(`/dl/windows?source=google&article=${publishedArticles[0]}`);
  expect(clickLink('/dl/linux-deb', 1).getAttribute('href')).toBe(`/dl/linux-deb?source=google&article=${publishedArticles[0]}`);
});

it('ignores unrelated, unavailable, and external links and all auth-page clicks', async () => {
  await render('/install');
  for (const href of ['/install', '/dl/mac-intel', 'https://other.example/dl/windows']) expect(clickLink(href).getAttribute('href')).toBe(href);
  await render('/auth/verify?token=secret');
  expect(clickLink('/dl/windows').getAttribute('href')).toBe('/dl/windows');
  expect(sessionStorage.getItem(ATTRIBUTION_KEY)).not.toContain('secret');
});

it('keeps links working when storage is unavailable', async () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
  await render(`/blog/${publishedArticles[0]}?utm_medium=email`);
  await render('/install');
  expect(clickLink('/dl/mac-arm').getAttribute('href')).toBe(`/dl/mac-arm?source=email&article=${publishedArticles[0]}`);
});

it('includes attribution in copied links and refreshes it for a context-menu download', async () => {
  const anchor = document.createElement('a'); anchor.href = '/dl/windows'; document.body.append(anchor);
  await render('/install?utm_source=reddit');
  expect(anchor.getAttribute('href')).toBe('/dl/windows?source=reddit');
  anchor.href = '/dl/windows';
  anchor.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }));
  expect(anchor.getAttribute('href')).toBe('/dl/windows?source=reddit');
  anchor.remove();
});
