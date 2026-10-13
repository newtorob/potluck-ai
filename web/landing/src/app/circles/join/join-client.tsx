'use client';

import { useEffect, useState } from 'react';
import { VerifyShell } from '../../auth/verify/verify-shell';

const buttonClass = 'rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50';

// Invite codes use an alphabet without 0, 1, I, L, O or U.
const CODE = /^[2-9A-HJKMNP-TV-Z]{12}$/;

/** The invite code from this page's address, or ''. It rides in the fragment
 *  (`#code=ABCD-EFGH-JKMN`), which the browser never sends to a server, so
 *  opening an invite link does not put the code in our logs. */
export function codeFromHash(hash: string): string {
  const raw = new URLSearchParams(hash.replace(/^#/, '')).get('code') ?? '';
  const compact = raw.replace(/[\s-]/g, '').toUpperCase();
  return CODE.test(compact) ? `${compact.slice(0, 4)}-${compact.slice(4, 8)}-${compact.slice(8)}` : '';
}

export function JoinClient() {
  const [code, setCode] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const read = () => setCode(codeFromHash(window.location.hash));
    read();
    window.addEventListener('hashchange', read);
    return () => window.removeEventListener('hashchange', read);
  }, []);

  if (code === null) return <VerifyShell heading="Join a trusted circle">
    <p className="text-sm text-muted-foreground">Reading your invite…</p>
  </VerifyShell>;

  if (code === '') return <VerifyShell heading="This invite link is incomplete">
    <p className="text-sm text-muted-foreground">
      The link should end with an invite code. Ask the person who invited you to send it again, or paste the code they gave you into Potluck under Trusted circles.
    </p>
  </VerifyShell>;

  async function copy() {
    try {
      await navigator.clipboard.writeText(code ?? '');
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return <VerifyShell heading="You are invited to a trusted circle">
    <p className="text-sm text-muted-foreground">
      A trusted circle is a group of people who lend each other spare computer time for AI. Nothing happens until you accept the invite in Potluck.
    </p>
    <div className="w-full rounded-lg border border-border px-4 py-3">
      <p className="text-xs text-muted-foreground">Your invite code</p>
      <p className="mt-1 font-mono text-2xl tracking-widest text-foreground" data-testid="invite-code">{code}</p>
    </div>
    <button className={buttonClass} type="button" onClick={() => void copy()}>{copied ? 'Copied' : 'Copy code'}</button>
    <ol className="w-full list-decimal space-y-2 pl-5 text-left text-sm text-muted-foreground">
      <li>Open Potluck on your computer. If you do not have it yet, <a className="text-foreground underline underline-offset-4" href="/install">install it here</a> and sign in.</li>
      <li>Go to Network, then Trusted circles, and paste the code under Join a circle.</li>
      <li>Potluck shows who invited you before you join. From a terminal, run <span className="font-mono text-foreground">potluck circle join {code}</span>.</li>
    </ol>
    <p className="text-left text-xs text-muted-foreground">
      Joining lets the people in the circle send requests to your machines that share with trusted circles, and lets you send requests to theirs. The machine that answers sees the conversation. You can leave at any time. The code works once.
    </p>
  </VerifyShell>;
}
