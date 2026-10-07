import { describe, expect, it } from 'vitest';
import { classifyChatError } from './chatErrorRecovery';
import { networkRefusalCopy } from './networkRefusal';

describe('networkRefusalCopy', () => {
  it('tells a circle requester what happened and what to do, without API wording', () => {
    const copy = networkRefusalCopy('circle', 503, 'qwen3-4b-q4');
    expect(copy).toContain('No machine in this circle is offering qwen3-4b-q4');
    expect(copy).not.toContain('scope=');
    expect(classifyChatError(copy).action).toBe('check-network');
  });

  it('names an expired sign-in for either network scope', () => {
    expect(networkRefusalCopy('circle', 401, 'm')).toContain('Sign in again');
    expect(networkRefusalCopy('cooperative', 401, 'm')).toContain('Sign in again');
  });

  it('says a deleted circle is gone', () => {
    expect(networkRefusalCopy('circle', 404, 'm')).toContain('no longer exists');
  });

  it('keeps the coordinator line when it has nothing better', () => {
    expect(networkRefusalCopy('circle', 500, 'm')).toBeNull();
    expect(networkRefusalCopy('cooperative', 404, 'm')).toBeNull();
  });
});
