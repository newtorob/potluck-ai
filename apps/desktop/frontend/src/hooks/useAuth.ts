/**
 * useAuth — manages the magic-link auth flow and session state.
 *
 * On mount, attempts to rehydrate session by calling cloudApi.getMe().
 * The session persists as an httpOnly cookie (dev) or a main-process bearer
 * token (packaged app); if it is still valid the server returns the profile,
 * otherwise the error is swallowed and the user is treated as signed out.
 */

import { useEffect, useCallback } from 'react';
import { useAuthStore } from '@/stores/authStore';
import { cloudApi } from '@/lib/cloudApi';

// Silent session rehydration should run once per app session, not once per
// useAuth() consumer. Multiple components call useAuth() (AuthGate, the
// membership surfaces); this module-level flag makes the getMe() probe fire a
// single time. Effects run sequentially during commit, so setting it at the top
// of the first effect safely blocks the rest.
let rehydrateAttempted = false;

interface UseAuthReturn {
  session: ReturnType<typeof useAuthStore.getState>['session'];
  memberProfile: ReturnType<typeof useAuthStore.getState>['memberProfile'];
  /**
   * A verified-but-unconfirmed session awaiting the "Sign in as <email>?"
   * prompt (see SignInConfirmDialog). Non-null means a deep link just
   * exchanged a token and is waiting on the user before anything persists.
   */
  pendingSession: ReturnType<typeof useAuthStore.getState>['pendingSession'];
  isAuthLoading: boolean;
  authError: string | null;
  isSignedIn: boolean;
  requestMagicLink: (email: string) => Promise<void>;
  /** Exchanges the token but does NOT sign in — populates `pendingSession` for confirmation. */
  verifyMagicLink: (token: string) => Promise<void>;
  /** User confirmed the account shown for `pendingSession` — persist it and sign in. */
  confirmSignIn: () => Promise<void>;
  /** User declined — discard `pendingSession` without ever persisting it. */
  cancelSignIn: () => Promise<void>;
  signOut: () => Promise<void>;
}

export function useAuth(): UseAuthReturn {
  const store = useAuthStore();

  // Attempt silent session rehydration on mount (once per app session).
  //
  // The result is applied to the global auth store unconditionally — NOT gated
  // on a component-level "cancelled" flag. Under React.StrictMode the initiating
  // component is mounted, unmounted, and remounted immediately; the module-level
  // guard above stops the remount from re-fetching, so a cancel-on-unmount guard
  // would discard the only in-flight getMe() and leave a valid session rendering
  // as signed-out. The store is global, so a late write after unmount is safe.
  useEffect(() => {
    if (rehydrateAttempted) return;
    rehydrateAttempted = true;

    store.setAuthLoading(true);
    cloudApi
      .getMe()
      .then((profile) => {
        // getMe succeeded => the session cookie is valid; synthesise a minimal
        // session object for the store (the real token lives server-side).
        store.setSession(
          { token: '', expiresAt: '', memberId: profile.id },
          profile,
        );
      })
      .catch(() => {
        // Not signed in — expected; treat as unauthenticated.
      })
      .finally(() => {
        store.setAuthLoading(false);
      });
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

  // Exchanges the one-time token (consumes it server-side) but stops short of
  // signing in: the result lands in `pendingSession` for the caller to show
  // an explicit "Sign in as <email>?" prompt (SignInConfirmDialog) before
  // anything is persisted. This is the login-CSRF fix — see cloudApi.verifyMagicLink.
  const verifyMagicLink = useCallback(
    async (token: string) => {
      store.setAuthLoading(true);
      store.setAuthError(null);
      try {
        const session = await cloudApi.verifyMagicLink(token);
        store.setPendingSession(session);
      } catch (err) {
        store.setAuthError(err instanceof Error ? err.message : 'Verification failed');
        throw err;
      } finally {
        store.setAuthLoading(false);
      }
    },
    [store],
  );

  // User confirmed the account shown in the prompt: persist the session
  // (cloud.setToken) and fetch the profile. This is the ONLY path that turns
  // a pendingSession into an active one.
  const confirmSignIn = useCallback(async () => {
    const pending = store.pendingSession;
    if (!pending) return;
    store.setAuthLoading(true);
    store.setAuthError(null);
    try {
      await cloudApi.commitSession(pending);
      const profile = await cloudApi.getMe();
      store.setSession(pending, profile);
    } catch (err) {
      store.setAuthError(err instanceof Error ? err.message : 'Sign-in failed');
      throw err;
    } finally {
      store.setAuthLoading(false);
      store.setPendingSession(null);
    }
  }, [store]);

  // User declined: drop the pending session without ever persisting it. For
  // the cookie transport (dev/browser) the exchange call already set an
  // httpOnly cookie server-side, so also revoke it explicitly — "cancel"
  // must mean signed into nothing on every transport.
  const cancelSignIn = useCallback(async () => {
    if (!store.pendingSession) return;
    store.setPendingSession(null);
    try {
      await cloudApi.logout();
    } catch {
      // Best-effort revoke; nothing was persisted client-side regardless.
    }
  }, [store]);

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
    pendingSession: store.pendingSession,
    isAuthLoading: store.isAuthLoading,
    authError: store.authError,
    isSignedIn: store.session !== null,
    requestMagicLink,
    verifyMagicLink,
    confirmSignIn,
    cancelSignIn,
    signOut,
  };
}
