// Global test setup: register jest-dom matchers and unmount React trees
// between tests so component state never leaks across cases.
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// jsdom 29 omits a working localStorage unless --localstorage-file is
// provided - depending on the platform it is either undefined or an object
// with no methods, so probe getItem rather than mere existence.
const existing = globalThis.localStorage as Storage | undefined;
if (typeof existing?.getItem !== 'function') {
  const data = new Map<string, string>();
  const stub: Storage = {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => {
      data.set(k, String(v));
    },
    removeItem: (k: string) => {
      data.delete(k);
    },
    clear: () => data.clear(),
    get length() {
      return data.size;
    },
    key: (i: number) => [...data.keys()][i] ?? null,
  };
  Object.defineProperty(globalThis, 'localStorage', { value: stub, configurable: true });
  if (typeof window !== 'undefined' && window !== (globalThis as unknown as Window)) {
    Object.defineProperty(window, 'localStorage', { value: stub, configurable: true });
  }
}

afterEach(() => {
  cleanup();
});
