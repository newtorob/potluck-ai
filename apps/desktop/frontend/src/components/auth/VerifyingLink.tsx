/**
 * VerifyingLink — the "check your email" state shown after a magic link is sent.
 *
 * The actual token arrives out-of-band via a potluck:// deep link handled
 * globally by useDeepLinkAuth, so this screen is presentational: it tells the
 * user to open the email, reflects the verifying spinner while a token is being
 * exchanged, and offers resend / change-email actions.
 */

import { useAuth } from '@/hooks/useAuth';

interface VerifyingLinkProps {
  email: string;
  onResend: () => void;
  onChangeEmail: () => void;
}

export function VerifyingLink({ email, onResend, onChangeEmail }: VerifyingLinkProps) {
  const { isAuthLoading, authError } = useAuth();

  return (
    <div className="w-full space-y-5 text-center">
      <div className="flex flex-col items-center gap-3">
        <div className="flex h-11 w-11 items-center justify-center rounded-full bg-primary/12 ring-1 ring-primary/20">
          {isAuthLoading ? (
            <span
              className="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent"
              role="status"
              aria-label="Verifying"
            />
          ) : (
            <span className="h-2 w-2 rounded-full bg-primary animate-hearth-pulse" aria-hidden="true" />
          )}
        </div>
        <div className="space-y-1">
          <p className="text-sm font-medium text-foreground">
            {isAuthLoading ? 'Signing you in…' : 'Check your email'}
          </p>
          <p className="text-sm text-muted-foreground">
            We sent a sign-in link to <span className="text-foreground">{email}</span>. Open it on
            this device and Potluck will finish signing you in.
          </p>
        </div>
      </div>

      {authError && (
        <p role="alert" className="text-xs text-destructive">
          {authError}
        </p>
      )}

      <div className="flex items-center justify-center gap-4 text-xs">
        <button
          type="button"
          onClick={onResend}
          className="text-primary transition-colors hover:text-primary/80"
        >
          Resend link
        </button>
        <span className="text-border" aria-hidden="true">
          ·
        </span>
        <button
          type="button"
          onClick={onChangeEmail}
          className="text-muted-foreground transition-colors hover:text-foreground"
        >
          Use a different email
        </button>
      </div>
    </div>
  );
}
