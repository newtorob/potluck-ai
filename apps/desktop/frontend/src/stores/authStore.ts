/**
 * Auth store — session and member profile.
 *
 * Session is null until the user signs in via magic link.
 * Populated by useAuth hook which calls cloudApi.getMe() on mount.
 *
 * NOTE: The session token itself lives in an httpOnly cookie managed by
 * the cloud API — this store holds only the metadata for UI rendering.
 */

import { create } from 'zustand';
import type { Session, MemberProfile } from '@/types/api';

interface AuthState {
  session: Session | null;
  memberProfile: MemberProfile | null;
  isAuthLoading: boolean;
  authError: string | null;
  /**
   * A session that has been exchanged (verifyMagicLink resolved) but not yet
   * confirmed by the user. Non-null here is what drives the "Sign in as
   * <email>?" prompt — see useAuth.confirmSignIn/cancelSignIn. Nothing reads
   * or trusts this as an active session; only `session` above is.
   */
  pendingSession: Session | null;

  /**
   * Whether a cloud session token is persisted (packaged app only — see
   * cloudApi.hasToken). `null` means "unknown/not applicable", which is the
   * permanent state on the dev/browser cookie transport. Set once by the
   * silent rehydration probe in useAuth.
   */
  hasPersistedToken: boolean | null;

  /**
   * Why the last rehydration probe (getMe()) failed to produce a session,
   * when it did. `'network'` = couldn't reach the cloud API (offline, DNS,
   * 5xx, timeout) — the token itself may still be good. `'auth'` = the API
   * was reached and said the token is invalid/expired (401/403). `null` =
   * no failure recorded yet (still loading, or the probe succeeded).
   */
  authFailureKind: 'network' | 'auth' | null;

  // Actions
  setSession: (session: Session, profile: MemberProfile) => void;
  clearSession: () => void;
  setPendingSession: (session: Session | null) => void;
  setAuthLoading: (loading: boolean) => void;
  setAuthError: (error: string | null) => void;
  setHasPersistedToken: (hasToken: boolean | null) => void;
  setAuthFailureKind: (kind: 'network' | 'auth' | null) => void;
  updateProfile: (profile: Partial<MemberProfile>) => void;
}

export const useAuthStore = create<AuthState>()((set) => ({
  session: null,
  memberProfile: null,
  isAuthLoading: false,
  authError: null,
  pendingSession: null,
  hasPersistedToken: null,
  authFailureKind: null,

  setSession(session, memberProfile) {
    set({ session, memberProfile, authError: null, authFailureKind: null });
  },

  clearSession() {
    set({ session: null, memberProfile: null });
  },

  setPendingSession(pendingSession) {
    set({ pendingSession });
  },

  setAuthLoading(isAuthLoading) {
    set({ isAuthLoading });
  },

  setAuthError(authError) {
    set({ authError });
  },

  setHasPersistedToken(hasPersistedToken) {
    set({ hasPersistedToken });
  },

  setAuthFailureKind(authFailureKind) {
    set({ authFailureKind });
  },

  updateProfile(patch) {
    set((s) =>
      s.memberProfile
        ? { memberProfile: { ...s.memberProfile, ...patch } }
        : {},
    );
  },
}));
