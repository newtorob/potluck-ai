// Global test setup: register jest-dom matchers and unmount React trees
// between tests so component state never leaks across cases.
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// jsdom 29 can omit localStorage unless --localstorage-file is provided.
if (typeof globalThis.localStorage === 'undefined' || !globalThis.localStorage) {
  const data = new Map<string, string>();
  globalThis.localStorage = {
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
}

afterEach(() => {
  cleanup();
});
