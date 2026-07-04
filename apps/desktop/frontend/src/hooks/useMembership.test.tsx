import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

// Mock the cloud client so the hook's mount-time getMembership() resolves and
// the billing actions can be asserted without a network.
vi.mock('@/lib/cloudApi', () => ({
  cloudApi: {
    getMembership: vi.fn().mockResolvedValue({
      plan: null,
      status: 'none',
      isFoundingMember: false,
      currentPeriodEnd: null,
    }),
    createCheckout: vi.fn().mockResolvedValue({ url: 'https://stripe/checkout' }),
    getBillingPortal: vi.fn().mockResolvedValue({ url: 'https://stripe/portal' }),
  },
}));

import { cloudApi } from '@/lib/cloudApi';
import { useMembership } from '@/hooks/useMembership';

describe('useMembership', () => {
  beforeEach(() => {
    vi.stubGlobal('open', vi.fn());
  });
  afterEach(() => vi.unstubAllGlobals());

  it('loads membership on mount', async () => {
    const { result } = renderHook(() => useMembership());
    await waitFor(() => expect(result.current.membership).not.toBeNull());
    expect(cloudApi.getMembership).toHaveBeenCalled();
    expect(result.current.membership?.status).toBe('none');
  });

  it('startCheckout creates a checkout and opens the returned url', async () => {
    const { result } = renderHook(() => useMembership());
    await act(async () => {
      await result.current.startCheckout('founding');
    });
    expect(cloudApi.createCheckout).toHaveBeenCalledWith('founding');
    expect(window.open).toHaveBeenCalledWith('https://stripe/checkout', '_blank', 'noopener,noreferrer');
  });

  it('openBillingPortal opens the portal url', async () => {
    const { result } = renderHook(() => useMembership());
    await act(async () => {
      await result.current.openBillingPortal();
    });
    expect(cloudApi.getBillingPortal).toHaveBeenCalled();
    expect(window.open).toHaveBeenCalledWith('https://stripe/portal', '_blank', 'noopener,noreferrer');
  });
});
