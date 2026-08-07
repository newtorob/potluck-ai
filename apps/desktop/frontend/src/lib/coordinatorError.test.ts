import { describe, it, expect } from 'vitest';
import {
  classifyCoordinatorResult,
  classifyCoordinatorError,
  defaultCoordinatorErrorCopy,
} from '@/lib/coordinatorError';

describe('classifyCoordinatorResult', () => {
  it('401 -> unauthenticated', () => {
    const state = classifyCoordinatorResult({ status: 401, body: { detail: 'Not authenticated' } });
    expect(state).toEqual({ kind: 'unauthenticated', status: 401, detail: 'Not authenticated' });
  });

  it('403 -> forbidden', () => {
    const state = classifyCoordinatorResult({ status: 403, body: { detail: 'Invalid coordinator token' } });
    expect(state).toEqual({ kind: 'forbidden', status: 403, detail: 'Invalid coordinator token' });
  });

  it('status 0 -> unreachable', () => {
    const state = classifyCoordinatorResult({ status: 0, body: { detail: 'network error' } });
    expect(state).toEqual({ kind: 'unreachable', status: 0, detail: 'network error' });
  });

  it('500 -> server_error', () => {
    const state = classifyCoordinatorResult({ status: 500, body: { detail: 'Internal Server Error' } });
    expect(state).toEqual({ kind: 'server_error', status: 500, detail: 'Internal Server Error' });
  });

  it('503 -> server_error', () => {
    const state = classifyCoordinatorResult({ status: 503, body: null });
    expect(state).toEqual({ kind: 'server_error', status: 503, detail: 'HTTP 503' });
  });

  it('2xx -> ok', () => {
    expect(classifyCoordinatorResult({ status: 200, body: {} })).toEqual({ kind: 'ok' });
    expect(classifyCoordinatorResult({ status: 204, body: null })).toEqual({ kind: 'ok' });
  });

  it('409 -> generic error, carrying the coordinator detail', () => {
    const state = classifyCoordinatorResult({ status: 409, body: { detail: 'Already a member' } });
    expect(state).toEqual({ kind: 'error', status: 409, detail: 'Already a member' });
  });

  it('falls back to a generic detail when the body has no detail field', () => {
    const state = classifyCoordinatorResult({ status: 500, body: { raw: 'oops' } });
    expect(state).toEqual({ kind: 'server_error', status: 500, detail: 'HTTP 500' });
  });
});

describe('classifyCoordinatorError', () => {
  it('parses the [localApi] {status}: {detail} shape thrown by fromSessionResult', () => {
    const err = new Error('[localApi] 401: Not authenticated');
    expect(classifyCoordinatorError(err)).toEqual({
      kind: 'unauthenticated',
      status: 401,
      detail: 'Not authenticated',
    });
  });

  it('parses a 403 the same way', () => {
    const err = new Error('[localApi] 403: Invalid coordinator token');
    expect(classifyCoordinatorError(err)).toEqual({
      kind: 'forbidden',
      status: 403,
      detail: 'Invalid coordinator token',
    });
  });

  it('parses a 5xx the same way', () => {
    const err = new Error('[localApi] 502: Bad Gateway');
    expect(classifyCoordinatorError(err)).toEqual({
      kind: 'server_error',
      status: 502,
      detail: 'Bad Gateway',
    });
  });

  it('treats a raw fetch/network error (no localApi status prefix) as unreachable', () => {
    const err = new TypeError('Failed to fetch');
    expect(classifyCoordinatorError(err)).toEqual({
      kind: 'unreachable',
      status: 0,
      detail: 'Failed to fetch',
    });
  });

  it('treats a non-Error throw as unreachable', () => {
    expect(classifyCoordinatorError('boom')).toEqual({
      kind: 'unreachable',
      status: 0,
      detail: 'unknown error',
    });
  });
});

describe('defaultCoordinatorErrorCopy', () => {
  it('gives forbidden a server-misconfiguration message, not a "signed out" or "unreachable" one', () => {
    const copy = defaultCoordinatorErrorCopy({ kind: 'forbidden', status: 403, detail: 'x' });
    expect(copy.title.toLowerCase()).toContain('credentials');
    expect(copy.title.toLowerCase()).not.toContain('signed out');
    expect(copy.title.toLowerCase()).not.toContain('reach');
  });

  it('gives unreachable a connection-check message', () => {
    const copy = defaultCoordinatorErrorCopy({ kind: 'unreachable', status: 0, detail: 'x' });
    expect(copy.title.toLowerCase()).toContain('reach');
  });

  it('gives server_error a try-again message', () => {
    const copy = defaultCoordinatorErrorCopy({ kind: 'server_error', status: 500, detail: 'x' });
    expect(copy.description.toLowerCase()).toContain('try again');
  });
});
