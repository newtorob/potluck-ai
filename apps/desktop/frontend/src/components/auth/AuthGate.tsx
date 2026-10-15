import type { ReactNode } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { useDeepLinkAuth } from '@/hooks/useDeepLinkAuth';
import { SignInConfirmDialog } from '@/components/auth/SignInConfirmDialog';
import { useUiPrefsStore } from '@/stores/uiPrefsStore';
import { continueLocally } from '@/lib/localUse';
import { SignInWall } from '@/components/auth/SignInWall';

interface AuthGateProps {
  children: ReactNode;
}

/** Resolves account access while keeping an explicit local-use choice available. */
export function AuthGate({ children }: AuthGateProps) {
  const { isAuthLoading, isSignedIn, hasCachedAccount } = useAuth();
  useDeepLinkAuth();

  const localUseEnabled = useUiPrefsStore((s) => s.localUseEnabled);

  if (!localUseEnabled && (isAuthLoading || (!isSignedIn && !hasCachedAccount))) {
    return (
      <>
        <SignInWall onContinueLocal={continueLocally} checkingAccount={isAuthLoading} />
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
