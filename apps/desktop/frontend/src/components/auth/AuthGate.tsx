import type { ReactNode } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { useDeepLinkAuth } from '@/hooks/useDeepLinkAuth';
import { SignInConfirmDialog } from '@/components/auth/SignInConfirmDialog';
import { SignInWall } from '@/components/auth/SignInWall';

interface AuthGateProps {
  children: ReactNode;
}

/**
 * AuthGate — resolves auth state, handles magic-link deep links, and
 * requires a signed-in account to use the app.
 *
 * An account is required, but Potluck must still work for a signed-in member
 * who is offline or hitting a cloud-API blip: local chat, memory, and MCP run
 * against the local sidecar and don't need the cloud API on every launch.
 * useAuth's rehydration probe distinguishes an auth failure (401/403 — the
 * token is invalid, show the wall) from a network failure (offline/5xx/
 * timeout — treat it as a cached account and let the app run). See
 * useAuth.hasCachedAccount for the underlying logic.
 *
 * Three states:
 *   1. isAuthLoading — the first getMe() probe hasn't resolved yet. Render a
 *      neutral loading screen; never flash the wall or the app first.
 *   2. Resolved, not signed in, no cached account — render the sign-in wall.
 *   3. Resolved, signed in OR resolved with a cached account (offline) —
 *      render children.
 *
 * useDeepLinkAuth and SignInConfirmDialog are mounted unconditionally in
 * every branch: the magic-link deep-link callback (and the "Sign in as
 * <email>?" confirmation it feeds) must keep working while the wall is
 * showing, so completing sign-in flips straight to the app with no reload.
 */
export function AuthGate({ children }: AuthGateProps) {
  const { isAuthLoading, isSignedIn, hasCachedAccount } = useAuth();
  useDeepLinkAuth();

  if (isAuthLoading) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-background">
        <div
          className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent"
          role="status"
          aria-label="Loading"
        />
        <SignInConfirmDialog />
      </div>
    );
  }

  if (!isSignedIn && !hasCachedAccount) {
    return (
      <>
        <SignInWall />
        <SignInConfirmDialog />
      </>
    );
  }

  return (
    <>
      {children}
      <SignInConfirmDialog />
    </>
  );
}
