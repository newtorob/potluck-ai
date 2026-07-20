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

  // Actions
  setSession: (session: Session, profile: MemberProfile) => void;
  clearSession: () => void;
  setPendingSession: (session: Session | null) => void;
  setAuthLoading: (loading: boolean) => void;
  setAuthError: (error: string | null) => void;
  updateProfile: (profile: Partial<MemberProfile>) => void;
}

export const useAuthStore = create<AuthState>()((set) => ({
  session: null,
  memberProfile: null,
  isAuthLoading: false,
  authError: null,
  pendingSession: null,

  setSession(session, memberProfile) {
    set({ session, memberProfile, authError: null });
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

  updateProfile(patch) {
    set((s) =>
      s.memberProfile
        ? { memberProfile: { ...s.memberProfile, ...patch } }
        : {},
    );
  },
}));
