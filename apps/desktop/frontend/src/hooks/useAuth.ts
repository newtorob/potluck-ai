/**
 * useAuth — manages the magic-link auth flow and session state.
 *
 * On mount, attempts to rehydrate session by calling cloudApi.getMe().
 * If the httpOnly cookie is still valid the server returns the profile;
 * otherwise the error is swallowed and the user is treated as signed out.
 */

import { useEffect, useCallback } from 'react';
import { useAuthStore } from '@/stores/authStore';
import { cloudApi } from '@/lib/cloudApi';

interface UseAuthReturn {
  session: ReturnType<typeof useAuthStore.getState>['session'];
  memberProfile: ReturnType<typeof useAuthStore.getState>['memberProfile'];
  isAuthLoading: boolean;
  authError: string | null;
  isSignedIn: boolean;
  requestMagicLink: (email: string) => Promise<void>;
  verifyMagicLink: (token: string) => Promise<void>;
  signOut: () => Promise<void>;
}

export function useAuth(): UseAuthReturn {
  const store = useAuthStore();

  // Attempt silent session rehydration on mount
  useEffect(() => {
    let cancelled = false;
    store.setAuthLoading(true);

    cloudApi
      .getMe()
      .then((profile) => {
        if (cancelled) return;
        // If getMe succeeds we have a valid session cookie; synthesise a
        // minimal session object for the store (token lives server-side).
        store.setSession(
          { token: '', expiresAt: '', memberId: profile.id },
          profile,
        );
      })
      .catch(() => {
        // Not signed in — expected; treat as unauthenticated
      })
      .finally(() => {
        if (!cancelled) store.setAuthLoading(false);
      });

    return () => {
      cancelled = true;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const requestMagicLink = useCallback(
    async (email: string) => {
      store.setAuthError(null);
      try {
        await cloudApi.requestMagicLink(email);
      } catch (err) {
        store.setAuthError(err instanceof Error ? err.message : 'Failed to send magic link');
        throw err;
      }
    },
    [store],
  );

  const verifyMagicLink = useCallback(
    async (token: string) => {
      store.setAuthLoading(true);
      store.setAuthError(null);
      try {
        const session = await cloudApi.verifyMagicLink(token);
        const profile = await cloudApi.getMe();
        store.setSession(session, profile);
      } catch (err) {
        store.setAuthError(err instanceof Error ? err.message : 'Verification failed');
        throw err;
      } finally {
        store.setAuthLoading(false);
      }
    },
    [store],
  );

  const signOut = useCallback(async () => {
    try {
      await cloudApi.logout();
    } finally {
      store.clearSession();
    }
  }, [store]);

  return {
    session: store.session,
    memberProfile: store.memberProfile,
    isAuthLoading: store.isAuthLoading,
    authError: store.authError,
    isSignedIn: store.session !== null,
    requestMagicLink,
    verifyMagicLink,
    signOut,
  };
}
