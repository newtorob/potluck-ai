import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useHealth } from './useHealth';

const health = vi.hoisted(() => vi.fn());
vi.mock('@/lib/localApi', () => ({ localApi: { health } }));
const loaded = { model_loaded: true, active_model_id: 'model', n_ctx_loaded: 8192 };
beforeEach(() => {
  health.mockReset().mockResolvedValue(loaded);
});
afterEach(() => {
  vi.useRealTimers();
});

describe('read-only engine health', () => {
  it('distinguishes checking from checked unavailable and retries only liveness', async () => {
    health.mockRejectedValueOnce(new Error('offline'));
    const { result } = renderHook(() => useHealth());
    expect(result.current.hasChecked).toBe(false);
    await waitFor(() => expect(result.current.hasChecked).toBe(true));
    expect(result.current.sidecarUp).toBe(false);
    act(() => result.current.refresh());
    expect(result.current.hasChecked).toBe(false);
    await waitFor(() => expect(result.current.sidecarUp).toBe(true));
    expect(result.current.activeModelId).toBe('model');
    expect(health).toHaveBeenCalledTimes(2);
  });

  it('ignores an old pending check after an explicit refresh', async () => {
    let resolve!: (value: typeof loaded) => void;
    health.mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const { result } = renderHook(() => useHealth());
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.sidecarUp).toBe(true));
    await act(async () => resolve({ ...loaded, active_model_id: 'old-model' }));
    expect(result.current.activeModelId).toBe('model');
  });

  it('retains last known model metadata on failure but marks the connection unavailable', async () => {
    const { result } = renderHook(() => useHealth());
    await waitFor(() => expect(result.current.sidecarUp).toBe(true));
    health.mockRejectedValueOnce(new Error('offline'));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.hasChecked).toBe(true));
    expect(result.current.sidecarUp).toBe(false);
    expect(result.current.activeModelId).toBe('model');
    expect(result.current.modelLoaded).toBe(true);
  });

  it('exposes startup restore metadata without treating it as a loaded model', async () => {
    health.mockResolvedValue({
      model_loaded: false,
      active_model_id: null,
      model_restore: { status: 'loading', model_id: 'saved-model' },
    });
    const { result } = renderHook(() => useHealth());
    await waitFor(() => expect(result.current.sidecarUp).toBe(true));
    expect(result.current.modelRestore?.status).toBe('loading');
    expect(result.current.modelLoaded).toBe(false);
    expect(result.current.activeModelId).toBeNull();
  });

  it('does not let an older interval response overwrite a newer visibility check', async () => {
    vi.useFakeTimers();
    let resolve!: (value: typeof loaded) => void;
    const { result, unmount } = renderHook(() => useHealth());
    await act(async () => {});
    health.mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    await act(async () => {
      vi.advanceTimersByTime(10000);
    });
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await act(async () => resolve({ ...loaded, active_model_id: 'old-model' }));
    expect(result.current.activeModelId).toBe('model');
    unmount();
    const calls = health.mock.calls.length;
    vi.advanceTimersByTime(20000);
    document.dispatchEvent(new Event('visibilitychange'));
    expect(health).toHaveBeenCalledTimes(calls);
  });
});
