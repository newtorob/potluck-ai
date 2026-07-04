// Global test setup: register jest-dom matchers and unmount React trees
// between tests so component state never leaks across cases.
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => {
  cleanup();
});
