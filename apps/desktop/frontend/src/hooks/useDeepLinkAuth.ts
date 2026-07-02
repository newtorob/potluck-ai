/**
 * useDeepLinkAuth — completes magic-link sign-in when the OS hands the app a
 * potluck://auth/callback?token=... deep link.
 *
 * The email link opens a web verify page which bounces to the potluck:// scheme;
 * Electron routes that back to this running app (see electron/main.js). The
 * token can arrive on ANY view, so this listener is mounted once globally (in
 * AuthGate), not tied to the sign-in screen being open.
 *
 * Cold start (app launched by the link) is covered by getInitialDeepLink();
 * links that arrive while the app runs are covered by onDeepLink().
 */

import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { ipc } from '@/lib/ipc';
import { useAuth } from '@/hooks/useAuth';

/** Pull the one-time token out of a potluck://auth/callback?token=... URL. */
function tokenFromDeepLink(url: string): string | null {
  try {
    return new URL(url).searchParams.get('token');
  } catch {
    const match = url.match(/[?&]token=([^&]+)/);
    return match?.[1] ? decodeURIComponent(match[1]) : null;
  }
}

export function useDeepLinkAuth(): void {
  const { verifyMagicLink } = useAuth();
  const navigate = useNavigate();
  // Each token is single-use; guard against double delivery (cold-start pull
  // plus a live event for the same URL).
  const processed = useRef<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;

    const handle = (url: string): void => {
      const token = tokenFromDeepLink(url);
      if (!token || processed.current.has(token)) return;
      processed.current.add(token);
      verifyMagicLink(token)
        .then(() => {
          if (cancelled) return;
          navigate('/membership');
          void ipc.showNotification(
            'Signed in to Potluck',
            'Your magic link worked.',
          );
        })
        .catch(() => {
          // authError is surfaced by the sign-in UI; nothing to do here.
        });
    };

    const unsubscribe = ipc.onDeepLink(handle);
    void ipc.getInitialDeepLink().then((url) => {
      if (!cancelled && url) handle(url);
    });

    return () => {
      cancelled = true;
      unsubscribe();
    };
    // Run once on mount; verifyMagicLink/navigate are stable enough for this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
