import type { Metadata } from 'next';
import { Suspense } from 'react';
import { VerifyClient } from './verify-client';
import { VerifyShell } from './verify-shell';

// The magic-link email points here: https://trypotluck.ai/auth/verify?token=...
// This page's only job is to hand the token to the running desktop app via the
// potluck:// deep link. It is not a destination to index or share.
export const metadata: Metadata = {
  title: 'Signing you in…',
  robots: { index: false, follow: false },
};

export default function AuthVerifyPage() {
  return (
    <Suspense fallback={<VerifyShell heading="Preparing…" />}>
      <VerifyClient />
    </Suspense>
  );
}
