'use client';

import { useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { VerifyShell } from './verify-shell';

/**
 * Bounce page for magic-link sign-in. Reads the one-time token from the URL and
 * redirects into the running desktop app via the potluck:// scheme, which the
 * Electron app registers (see apps/desktop/electron/main.js + useDeepLinkAuth).
 * The desktop app is what actually verifies the token against the cloud API.
 */
export function VerifyClient() {
  const token = useSearchParams().get('token');
  const deepLink = token ? `potluck://auth/callback?token=${encodeURIComponent(token)}` : null;
  const [bounced, setBounced] = useState(false);

  useEffect(() => {
    if (!deepLink) return;
    // Hand the token to the desktop app. The browser tab stays open (the OS
    // switches focus to Potluck), so we surface a manual fallback shortly after.
    window.location.href = deepLink;
    const t = setTimeout(() => setBounced(true), 1500);
    return () => clearTimeout(t);
  }, [deepLink]);

  if (!token) {
    return (
      <VerifyShell heading="This link didn't work">
        <p className="text-sm text-muted-foreground">
          It may have expired or already been used. Request a new sign-in link from the
          Potluck app.
        </p>
      </VerifyShell>
    );
  }

  return (
    <VerifyShell heading="Opening Potluck…">
      <p className="text-sm text-muted-foreground">
        We&rsquo;re handing your sign-in to the desktop app. You can close this tab once
        Potluck opens.
      </p>
      {bounced && (
        <a
          href={deepLink!}
          className="inline-flex items-center justify-center rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
        >
          Open Potluck
        </a>
      )}
    </VerifyShell>
  );
}
