/**
 * Login-CSRF regression tests (see useDeepLinkAuth.ts docstring).
 *
 * Covers the two halves of the fix:
 *   1. A valid potluck://auth/callback deep link exchanges its token but does
 *      NOT sign in on its own — no persisted session, no cloudApi.commitSession
 *      call — until something explicitly confirms it.
 *   2. A potluck:// URL outside the exact auth/callback host+path never even
 *      reaches token extraction.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

const deepLinkListeners: Array<(url: string) => void> = [];

vi.mock('@/lib/ipc', () => ({
  ipc: {
    onDeepLink: vi.fn((cb: (url: string) => void) => {
      deepLinkListeners.push(cb);
      return () => {
        const i = deepLinkListeners.indexOf(cb);
        if (i >= 0) deepLinkListeners.splice(i, 1);
      };
    }),
    getInitialDeepLink: vi.fn().mockResolvedValue(null),
    showNotification: vi.fn().mockResolvedValue(undefined),
  },
}));

vi.mock('@/lib/cloudApi', () => ({
  cloudApi: {
    verifyMagicLink: vi.fn(),
    commitSession: vi.fn().mockResolvedValue(undefined),
    getMe: vi.fn(),
    logout: vi.fn().mockResolvedValue(undefined),
    requestMagicLink: vi.fn(),
  },
}));

import { cloudApi } from '@/lib/cloudApi';
import { useAuthStore } from '@/stores/authStore';
import { useDeepLinkAuth } from '@/hooks/useDeepLinkAuth';

function emitDeepLink(url: string): void {
  deepLinkListeners.forEach((cb) => cb(url));
}

describe('useDeepLinkAuth — login CSRF gate', () => {
  beforeEach(() => {
    deepLinkListeners.length = 0;
    useAuthStore.setState({
      session: null,
      memberProfile: null,
      pendingSession: null,
      isAuthLoading: false,
      authError: null,
    });
    vi.mocked(cloudApi.verifyMagicLink).mockReset();
    vi.mocked(cloudApi.commitSession).mockReset().mockResolvedValue(undefined);
    // useAuth() also runs a silent getMe() rehydration probe on mount (once
    // per test file, guarded by a module-level flag) — unrelated to this
    // test, but must resolve/reject rather than being left undefined.
    vi.mocked(cloudApi.getMe).mockReset().mockRejectedValue(new Error('not signed in'));
  });

  afterEach(() => vi.clearAllMocks());

  it('exchanges a valid potluck://auth/callback token but does not sign in without confirmation', async () => {
    vi.mocked(cloudApi.verifyMagicLink).mockResolvedValue({
      token: 'session-token',
      expiresAt: '2099-01-01T00:00:00Z',
      memberId: 'member-1',
      email: 'victim@example.com',
    });

    renderHook(() => useDeepLinkAuth());
    emitDeepLink('potluck://auth/callback?token=one-time-token');

    await waitFor(() => {
      expect(cloudApi.verifyMagicLink).toHaveBeenCalledWith('one-time-token');
    });

    // The exchange populates pendingSession — that's what drives the "Sign
    // in as <email>?" confirm dialog...
    await waitFor(() => {
      expect(useAuthStore.getState().pendingSession?.email).toBe('victim@example.com');
    });

    // ...but the app is NOT signed in, and nothing was persisted, until the
    // user explicitly confirms (confirmSignIn — exercised separately).
    expect(useAuthStore.getState().session).toBeNull();
    expect(cloudApi.commitSession).not.toHaveBeenCalled();
  });

  it('ignores a potluck:// URL outside the exact auth/callback path — no token extracted', async () => {
    renderHook(() => useDeepLinkAuth());

    // Different host, same query shape a forged link could use.
    emitDeepLink('potluck://evil.example/callback?token=stolen-1');
    // Right host, wrong path.
    emitDeepLink('potluck://auth/not-callback?token=stolen-2');
    // No host/path at all.
    emitDeepLink('potluck://?token=stolen-3');

    // Give any (incorrect) async handling a tick to surface before asserting.
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(cloudApi.verifyMagicLink).not.toHaveBeenCalled();
    expect(useAuthStore.getState().pendingSession).toBeNull();
  });
});
