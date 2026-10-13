import { describe, it, expect } from 'vitest';

import { classifyChatError } from './chatErrorRecovery';

/**
 * The chat bubble must not match on the substring "503" and offer "Load a
 * model" for every failure that shares the status code, including routing
 * failures that loading a model cannot fix.
 */
describe('classifyChatError', () => {
  describe('local model problems send you to the library', () => {
    const cases = [
      '[localApi] 503: No model loaded. POST /models/{slug}/load first.',
      '[localApi] 503: No local model loaded; load one or retry with X-Potluck-Scope: mine',
      'No model is loaded on this machine.',
    ];
    it.each(cases)('%s', (text) => {
      const recovery = classifyChatError(text);
      expect(recovery.action).toBe('load-model');
      expect(recovery.route).toBe('/models');
      expect(recovery.label).toBe('Load a model');
    });
  });

  describe('routing problems send you to the network view', () => {
    const cases = [
      "[localApi] 503: No online peer serves model 'qwen3-4b-instruct-2507-q4'",
      'The contributor disconnected before finishing.',
      'The contributor timed out before finishing.',
      '[localApi] 503: circle routing requires a configured coordinator URL',
      'No contributor peers connected',
      'open-pool routing is not configured (no coordinator URL)',
    ];
    it.each(cases)('%s', (text) => {
      const recovery = classifyChatError(text);
      expect(recovery.action).toBe('check-network');
      expect(recovery.route).toBe('/network');
      expect(recovery.label).toBe('Check routing');
    });
  });

  it('offers nothing for a bare 503 with no stated cause', () => {
    expect(classifyChatError('[localApi] 503: Service Unavailable').action).toBeNull();
  });

  it('offers nothing when the failure is unrelated to models or routing', () => {
    expect(classifyChatError('The connection closed before the reply finished.').action).toBeNull();
    expect(classifyChatError('An unexpected error occurred.').action).toBeNull();
  });

  it('does not treat a remote model problem as a local one', () => {
    // "did not have this model loaded" must not match a bare "not loaded"
    // substring and point at the local model library.
    const recovery = classifyChatError('The contributor did not have this model loaded.');
    expect(recovery.action).toBe('check-network');
  });

  it('prefers the local model when a message names both', () => {
    // Loading a model here is the repair the user can always complete.
    const recovery = classifyChatError(
      '[localApi] 503: No model loaded on this node, and no household peer matched.',
    );
    expect(recovery.action).toBe('load-model');
  });

  it('handles a missing or non-string message', () => {
    expect(classifyChatError(null).action).toBeNull();
    expect(classifyChatError(undefined).action).toBeNull();
    expect(classifyChatError('').action).toBeNull();
  });
});
