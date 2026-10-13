// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { RequestWorkbench } from './RequestWorkbench';

let host: HTMLDivElement;
let root: Root;
let reduced = false;
function button(name: string) {
  const element = [...host.querySelectorAll('button')].find(e => e.textContent === name);
  if (!element) throw new Error(`Missing button ${name}`);
  return element;
}
async function click(name: string) { await act(async () => button(name).click()); }
beforeEach(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers(); reduced = false;
  vi.stubGlobal('matchMedia', () => ({ matches: reduced }));
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
  await act(async () => root.render(createElement(RequestWorkbench)));
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it('cancels an in-flight route when the scope changes, then answers on the new scope', async () => {
  await click('Send example');
  expect(button('Running example').disabled).toBe(true);
  await click('Local');
  await act(async () => vi.advanceTimersByTime(3000));
  expect(host.querySelector('[role="status"]')?.textContent).toContain('Ready to answer on this laptop.');
  expect(host.querySelector('[role="status"]')?.textContent).not.toContain('Start with the bookshelf.');
  await click('Send example');
  await act(async () => vi.advanceTimersByTime(1200));
  expect(host.querySelector('[role="status"]')?.textContent).toContain('Answered on this laptop.');
});

it('honors reduced motion with an immediate complete result and no queued timers', async () => {
  reduced = true;
  await click('Work through code'); await click('Send example');
  expect(host.querySelector('[role="status"]')?.textContent).toContain('[0, 2, 4]');
  expect(host.querySelector('[role="status"]')?.textContent).toContain('Answered on your paired desktop.');
  expect(vi.getTimerCount()).toBe(0);
});

it('lets visitors skip the animation and clears timers on unmount', async () => {
  await click('Send example'); await click('Skip animation');
  expect(host.querySelector('[role="status"]')?.textContent).toContain('Answered on your paired desktop.');
  expect(vi.getTimerCount()).toBe(0);
  await click('Run again');
  await act(async () => root.render(null));
  expect(vi.getTimerCount()).toBe(0);
});
