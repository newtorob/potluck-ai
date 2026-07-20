/**
 * SignInConfirmDialog — "Sign in as <email>?" gate on the magic-link deep
 * link flow.
 *
 * SECURITY (login CSRF fix): useDeepLinkAuth exchanges a potluck://auth/
 * callback token unconditionally — a deep link is not proof the person who
 * clicked it requested this app's sign-in (an attacker can request a magic
 * link to their OWN email, wrap the token as a deep link, and get a victim
 * to click it). The exchange result lands in `pendingSession`; this dialog
 * is the only place that turns it into an active, persisted session. Confirm
 * calls confirmSignIn() (persists via cloud.setToken + navigates); Cancel
 * calls cancelSignIn() and the app signs into nothing.
 *
 * Rendered unconditionally by AuthGate; renders nothing while
 * pendingSession is null.
 */

import { useNavigate } from 'react-router-dom';
import { useState, type KeyboardEvent, type MouseEvent } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { ipc } from '@/lib/ipc';
import { cn } from '@/components/ui/cn';

export function SignInConfirmDialog() {
  const { pendingSession, confirmSignIn, cancelSignIn, isAuthLoading } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);

  if (!pendingSession) return null;

  const handleConfirm = async (): Promise<void> => {
    setError(null);
    try {
      await confirmSignIn();
      navigate('/membership');
      void ipc.showNotification('Signed in to Potluck', 'Your magic link worked.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed');
    }
  };

  const handleCancel = (): void => {
    setError(null);
    void cancelSignIn();
  };

  const handleBackdropClick = (e: MouseEvent<HTMLDivElement>): void => {
    if (e.target === e.currentTarget) handleCancel();
  };

  const handleKeyDown = (e: KeyboardEvent): void => {
    if (e.key === 'Escape') handleCancel();
  };

  // The /auth/callback response carries the account email when the cloud API
  // includes it; fall back to a generic prompt rather than ever assuming an
  // identity we can't show the user.
  const email = pendingSession.email;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      role="dialog"
      aria-modal="true"
      aria-labelledby="sign-in-confirm-title"
      onClick={handleBackdropClick}
      onKeyDown={handleKeyDown}
    >
      <div className="w-full max-w-sm rounded-xl border border-border bg-card p-5 shadow-xl">
        <h2 id="sign-in-confirm-title" className="mb-2 text-sm font-semibold text-foreground">
          {email ? `Sign in as ${email}?` : 'Confirm sign-in to Potluck?'}
        </h2>
        <p className="mb-4 text-xs text-muted-foreground">
          {email
            ? 'This link signs the app into the account above. Only confirm if you expected this.'
            : "This link signs the app into an account. Only confirm if you requested this magic link yourself."}
        </p>

        {error && (
          <p className="mb-3 text-xs text-destructive" role="alert">
            {error}
          </p>
        )}

        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => void handleConfirm()}
            disabled={isAuthLoading}
            className={cn(
              'flex-1 rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground',
              'hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            {isAuthLoading ? 'Signing in…' : 'Confirm'}
          </button>
          <button
            type="button"
            onClick={handleCancel}
            disabled={isAuthLoading}
            className={cn(
              'rounded-lg px-3 py-2 text-xs font-medium text-muted-foreground',
              'hover:bg-muted hover:text-foreground transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
