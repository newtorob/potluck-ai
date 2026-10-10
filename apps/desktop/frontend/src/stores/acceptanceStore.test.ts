import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ inferenceHealth: vi.fn() }));
vi.mock('@/lib/localApi', () => ({ localApi: { inferenceHealth: mocks.inferenceHealth } }));

import { useAcceptanceStore } from './acceptanceStore';

const FALLBACK_MESSAGE =
  'The GPU gave wrong answers for this model, so it is running on the CPU, which is slower. Updating the graphics driver usually fixes this.';
const body = (acceptance: Record<string, unknown> | undefined) => ({
  build: {},
  devices: [],
  loaded: null,
  benchmark: null,
  ...(acceptance ? { acceptance } : {}),
});
const passed = { status: 'passed', model_id: 'm', verdict: 'ok', fallback: null, message: null };
const running = { status: 'running', model_id: 'm', verdict: null, fallback: null, message: null };
const fellBack = {
  status: 'passed',
  model_id: 'm',
  verdict: 'gpu_wrong_output',
  fallback: { from: 'gpu', to: 'cpu', reason: 'gpu_wrong_output' },
  message: FALLBACK_MESSAGE,
};

beforeEach(() => {
  vi.useFakeTimers();
  mocks.inferenceHealth.mockReset();
  useAcceptanceStore.setState({ acceptance: null, dismissedKey: null });
});
afterEach(() => {
  vi.useRealTimers();
});

describe('acceptanceStore.refresh', () => {
  it('keeps the engine acceptance in the store', async () => {
    mocks.inferenceHealth.mockResolvedValue(body(fellBack));
    await useAcceptanceStore.getState().refresh();
    expect(useAcceptanceStore.getState().acceptance).toMatchObject({
      status: 'passed',
      fallback: { to: 'cpu' },
      message: FALLBACK_MESSAGE,
    });
  });

  it('leaves it null for an older engine that has the route but no key', async () => {
    mocks.inferenceHealth.mockResolvedValue(body(undefined));
    await useAcceptanceStore.getState().refresh();
    expect(useAcceptanceStore.getState().acceptance).toBeNull();
  });

  it('clears a result when the engine stops reporting one', async () => {
    mocks.inferenceHealth.mockResolvedValue(body(fellBack));
    await useAcceptanceStore.getState().refresh();
    mocks.inferenceHealth.mockResolvedValue(body(undefined));
    await useAcceptanceStore.getState().refresh();
    expect(useAcceptanceStore.getState().acceptance).toBeNull();
  });

  it('keeps what it knew, and throws nothing, when the route is missing or the engine is not up', async () => {
    mocks.inferenceHealth.mockResolvedValue(body(fellBack));
    await useAcceptanceStore.getState().refresh();
    mocks.inferenceHealth.mockRejectedValue(new Error('[localApi] 404: Not Found'));
    await expect(useAcceptanceStore.getState().refresh()).resolves.toBeUndefined();
    expect(useAcceptanceStore.getState().acceptance?.fallback).not.toBeNull();
    mocks.inferenceHealth.mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(useAcceptanceStore.getState().refresh()).resolves.toBeUndefined();
  });

  it('starts from null when the first read fails', async () => {
    mocks.inferenceHealth.mockRejectedValue(new Error('[localApi] 401: unauthorized'));
    await useAcceptanceStore.getState().refresh();
    expect(useAcceptanceStore.getState().acceptance).toBeNull();
  });

  it('shares a read that is already in flight', async () => {
    let finish: (v: unknown) => void = () => {};
    mocks.inferenceHealth.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const a = useAcceptanceStore.getState().refresh();
    const b = useAcceptanceStore.getState().refresh();
    finish(body(passed));
    await Promise.all([a, b]);
    expect(mocks.inferenceHealth).toHaveBeenCalledTimes(1);
  });

  it('asks again every few seconds while the check is running, and stops once it is not', async () => {
    mocks.inferenceHealth
      .mockResolvedValueOnce(body(running))
      .mockResolvedValueOnce(body(running))
      .mockResolvedValue(body(fellBack));
    await useAcceptanceStore.getState().refresh();
    expect(useAcceptanceStore.getState().acceptance?.status).toBe('running');
    await vi.advanceTimersByTimeAsync(3_000);
    expect(mocks.inferenceHealth).toHaveBeenCalledTimes(2);
    expect(useAcceptanceStore.getState().acceptance?.status).toBe('running');
    await vi.advanceTimersByTimeAsync(3_000);
    expect(mocks.inferenceHealth).toHaveBeenCalledTimes(3);
    expect(useAcceptanceStore.getState().acceptance?.fallback).not.toBeNull();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(mocks.inferenceHealth).toHaveBeenCalledTimes(3);
  });

  it('gives up on a check that never finishes after a couple of minutes', async () => {
    mocks.inferenceHealth.mockResolvedValue(body(running));
    await useAcceptanceStore.getState().refresh();
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    // The first read plus 40 follow-ups.
    expect(mocks.inferenceHealth).toHaveBeenCalledTimes(41);
  });

  it('does not stack timers when refresh is called while one is waiting', async () => {
    mocks.inferenceHealth.mockResolvedValue(body(running));
    await useAcceptanceStore.getState().refresh();
    await useAcceptanceStore.getState().refresh();
    expect(mocks.inferenceHealth).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(3_000);
    expect(mocks.inferenceHealth).toHaveBeenCalledTimes(3);
    mocks.inferenceHealth.mockResolvedValue(body(passed));
    await vi.advanceTimersByTimeAsync(3_000);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(mocks.inferenceHealth).toHaveBeenCalledTimes(4);
  });
});

describe('acceptanceStore.dismiss', () => {
  it('remembers the state it closed, and nothing when there is no result', async () => {
    useAcceptanceStore.getState().dismiss();
    expect(useAcceptanceStore.getState().dismissedKey).toBeNull();
    mocks.inferenceHealth.mockResolvedValue(body(fellBack));
    await useAcceptanceStore.getState().refresh();
    useAcceptanceStore.getState().dismiss();
    expect(useAcceptanceStore.getState().dismissedKey).toContain('gpu_wrong_output');
  });
});
