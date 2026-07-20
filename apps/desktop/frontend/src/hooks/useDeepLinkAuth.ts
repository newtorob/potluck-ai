/**
 * useDeepLinkAuth — starts magic-link sign-in when the OS hands the app a
 * potluck://auth/callback?token=... deep link.
 *
 * The email link opens a web verify page which bounces to the potluck:// scheme;
 * Electron routes that back to this running app (see electron/main.js). The
 * token can arrive on ANY view, so this listener is mounted once globally (in
 * AuthGate), not tied to the sign-in screen being open.
 *
 * Cold start (app launched by the link) is covered by getInitialDeepLink();
 * links that arrive while the app runs are covered by onDeepLink().
 *
 * SECURITY (login CSRF): this hook only EXCHANGES the token — it does not
 * sign in on its own. A deep link is not proof the person who clicked it
 * requested this app's sign-in: an attacker can request a magic link to
 * THEIR OWN email, wrap the token as potluck://auth/callback?token=..., and
 * get a victim to click it. verifyMagicLink() populates `pendingSession`;
 * SignInConfirmDialog (mounted alongside this hook in AuthGate) is what
 * shows "Sign in as <email>?" and gates the actual persist/navigate on the
 * user confirming it's the account they expect.
 */

import { useEffect, useRef } from 'react';
import { ipc } from '@/lib/ipc';
import { useAuth } from '@/hooks/useAuth';

/**
 * Pull the one-time token out of a deep link, but ONLY when it's exactly
 * potluck://auth/callback — never from any other potluck:// URL. Scoping
 * extraction to this one path keeps every other potluck:// route (and any
 * scheme the OS mis-routes to us) from being able to trigger a token
 * exchange at all, regardless of query string.
 */
function tokenFromDeepLink(url: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'potluck:') return null;
  // Electron/WHATWG URL parsing of `scheme://host/path` puts "auth" in
  // hostname and "/callback" in pathname for this non-special scheme.
  if (parsed.hostname !== 'auth' || parsed.pathname !== '/callback') return null;
  return parsed.searchParams.get('token');
}

export function useDeepLinkAuth(): void {
  const { verifyMagicLink } = useAuth();
  // Each token is single-use; guard against double delivery (cold-start pull
  // plus a live event for the same URL).
  const processed = useRef<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;

    const handle = (url: string): void => {
      const token = tokenFromDeepLink(url);
      if (!token || processed.current.has(token)) return;
      processed.current.add(token);
      verifyMagicLink(token).catch(() => {
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
    // Run once on mount; verifyMagicLink is stable enough for this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
