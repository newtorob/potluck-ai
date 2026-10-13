/**
 * AuthVerify - the /auth/verify?token=... landing page the magic-link email
 * points at. It is a bounce page, not part of the app shell.
 *
 * Opened in the user's browser, it redirects to potluck://auth/callback?token=…
 * so the OS hands the token to the running desktop app (see useDeepLinkAuth +
 * electron/main.js). If it happens to load inside Electron, it just verifies in
 * place. The desktop SPA carries this route mainly for dev (app_url is the Vite
 * server); production hosts the equivalent page on the marketing site.
 *
 * Security (login CSRF): this route is reached by a ?token=... in the
 * URL, same as the deep-link path - verifyMagicLink()
 * exchanges the token but does not sign in on its own (see useAuth /
 * cloudApi.verifyMagicLink). This page renders its own "Sign in as <email>?"
 * confirm step (rather than relying on SignInConfirmDialog, which lives
 * inside AuthGate - a component tree this standalone route is rendered
 * outside of) before calling confirmSignIn().
 */

import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ipc } from '@/lib/ipc';
import { useAuth } from '@/hooks/useAuth';
import { PotluckLogo } from '@/components/brand/PotluckLogo';

type Status = 'working' | 'verifying' | 'confirming' | 'done' | 'error' | 'cancelled';

export function AuthVerify() {
  const [params] = useSearchParams();
  const token = params.get('token');
  const { verifyMagicLink, confirmSignIn, cancelSignIn, pendingSession, isAuthLoading } =
    useAuth();
  const navigate = useNavigate();
  const [status, setStatus] = useState<Status>('working');

  const deepLink = token
    ? `potluck://auth/callback?token=${encodeURIComponent(token)}`
    : null;

  useEffect(() => {
    if (!token) {
      setStatus('error');
      return;
    }
    if (ipc.isElectron) {
      // Already inside the app - exchange the token, then wait for the user
      // to confirm the account below before it's persisted.
      setStatus('verifying');
      verifyMagicLink(token)
        .then(() => setStatus('confirming'))
        .catch(() => setStatus('error'));
    } else if (deepLink) {
      // In a browser - bounce into the desktop app.
      window.location.href = deepLink;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const handleConfirm = async (): Promise<void> => {
    try {
      await confirmSignIn();
      setStatus('done');
      navigate('/membership');
    } catch {
      setStatus('error');
    }
  };

  const handleCancel = async (): Promise<void> => {
    await cancelSignIn();
    setStatus('cancelled');
  };

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-background p-8">
      <div className="flex w-full max-w-sm flex-col items-center gap-8 text-center animate-fade-in-up">
        <div className="relative h-16 w-16">
          <div
            className="absolute inset-0 rounded-2xl bg-primary/15 blur-xl"
            aria-hidden="true"
          />
          <div className="relative flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/12 ring-1 ring-primary/20">
            <PotluckLogo size={36} className="text-primary" aria-label="Potluck" />
          </div>
        </div>

        {status === 'error' ? (
          <div className="space-y-2">
            <h1 className="font-serif text-2xl tracking-display text-foreground">
              This link didn't work
            </h1>
            <p className="text-sm text-muted-foreground">
              It may have expired or already been used. Request a new one from
              the Potluck app.
            </p>
          </div>
        ) : status === 'cancelled' ? (
          <div className="space-y-2">
            <h1 className="font-serif text-2xl tracking-display text-foreground">
              Sign-in cancelled
            </h1>
            <p className="text-sm text-muted-foreground">
              You weren't signed in. Request a new link if this wasn't you.
            </p>
          </div>
        ) : status === 'confirming' ? (
          <div className="w-full space-y-4">
            <h1 className="font-serif text-2xl tracking-display text-foreground">
              {pendingSession?.email
                ? `Sign in as ${pendingSession.email}?`
                : 'Confirm sign-in to Potluck?'}
            </h1>
            <p className="text-sm text-muted-foreground">
              {pendingSession?.email
                ? 'Only confirm if you expected this.'
                : "Only confirm if you requested this magic link yourself."}
            </p>
            <div className="flex w-full gap-2">
              <button
                type="button"
                onClick={() => void handleConfirm()}
                disabled={isAuthLoading}
                className="flex-1 rounded-xl bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {isAuthLoading ? 'Signing in…' : 'Confirm'}
              </button>
              <button
                type="button"
                onClick={() => void handleCancel()}
                disabled={isAuthLoading}
                className="flex-1 rounded-xl px-4 py-2.5 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : status === 'done' ? (
          <div className="space-y-2">
            <h1 className="font-serif text-2xl tracking-display text-foreground">
              You're signed in
            </h1>
            <p className="text-sm text-muted-foreground">
              You can return to Potluck.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            <h1 className="font-serif text-2xl tracking-display text-foreground">
              Opening Potluck…
            </h1>
            <p className="text-sm text-muted-foreground">
              We're handing your sign-in to the desktop app.
            </p>
            {deepLink && !ipc.isElectron && (
              <a
                href={deepLink}
                className="inline-flex h-11 items-center justify-center rounded-xl bg-primary px-6 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                Open Potluck
              </a>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
