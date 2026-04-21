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

  // Actions
  setSession: (session: Session, profile: MemberProfile) => void;
  clearSession: () => void;
  setAuthLoading: (loading: boolean) => void;
  setAuthError: (error: string | null) => void;
  updateProfile: (profile: Partial<MemberProfile>) => void;
}

export const useAuthStore = create<AuthState>()((set) => ({
  session: null,
  memberProfile: null,
  isAuthLoading: false,
  authError: null,

  setSession(session, memberProfile) {
    set({ session, memberProfile, authError: null });
  },

  clearSession() {
    set({ session: null, memberProfile: null });
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
