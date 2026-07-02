/**
 * AuthVerify — the /auth/verify?token=... landing page the magic-link email
 * points at. It is a bounce page, not part of the app shell.
 *
 * Opened in the user's browser, it redirects to potluck://auth/callback?token=…
 * so the OS hands the token to the running desktop app (see useDeepLinkAuth +
 * electron/main.js). If it happens to load INSIDE Electron, it just verifies in
 * place. The desktop SPA carries this route mainly for dev (app_url is the Vite
 * server); production hosts the equivalent page on the marketing site.
 */

import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ipc } from '@/lib/ipc';
import { useAuth } from '@/hooks/useAuth';
import { PotluckLogo } from '@/components/brand/PotluckLogo';

type Status = 'working' | 'verifying' | 'done' | 'error';

export function AuthVerify() {
  const [params] = useSearchParams();
  const token = params.get('token');
  const { verifyMagicLink } = useAuth();
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
      // Already inside the app — verify directly, no bounce needed.
      setStatus('verifying');
      verifyMagicLink(token)
        .then(() => {
          setStatus('done');
          navigate('/membership');
        })
        .catch(() => setStatus('error'));
    } else if (deepLink) {
      // In a browser — bounce into the desktop app.
      window.location.href = deepLink;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

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
