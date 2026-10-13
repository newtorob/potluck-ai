'use client';

import { useEffect, useRef, useState, type FormEvent } from 'react';
import { VerifyShell } from '../verify/verify-shell';

const API = process.env.NEXT_PUBLIC_API_URL || 'https://api.trypotluck.ai';
// The code typed before the approval email, so the emailed link can fill it in.
// Only this browser has it, which keeps someone else's emailed link from
// approving a terminal on its own.
const PENDING_KEY = 'potluck.device.pending';
const PENDING_TTL_MS = 15 * 60 * 1000;

const inputClass = 'mt-2 w-full rounded-lg border border-border bg-background px-3 py-2 text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring';
const buttonClass = 'rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50';
const quietClass = 'rounded-lg px-4 py-2 text-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50';

type Phase = 'loading' | 'confirm' | 'request' | 'sent' | 'review' | 'approved' | 'denied';

class RequestError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

function messageFor(status: number, error: unknown): string {
  if (status === 429) return 'Too many attempts. Wait a minute and try again.';
  if (error === 'invalid_user_code') return 'That code does not match. Enter the code shown in your terminal.';
  if (error === 'forbidden_origin') return 'Open this page at trypotluck.ai/auth/device and try again.';
  if (error === 'not_signed_in') return 'This browser is no longer signed in. Enter your email to approve by email.';
  return 'This request expired or was already used. Run potluck login again to start a new request.';
}

async function call(path: string, body?: object, method: 'GET' | 'POST' = 'POST'): Promise<Record<string, unknown>> {
  const init: RequestInit = {
    method, credentials: 'include', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(15000),
  };
  if (body) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`${API}${path}`, init);
  const result = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) throw new RequestError(messageFor(response.status, result.error), response.status);
  return result;
}

function displayCode(raw: string): string {
  const compact = raw.replace(/[^0-9A-Za-z]/g, '').toUpperCase();
  return compact.length === 8 ? `${compact.slice(0, 4)}-${compact.slice(4)}` : raw.toUpperCase();
}

function rememberCode(code: string) {
  try { localStorage.setItem(PENDING_KEY, JSON.stringify({ code, at: Date.now() })); } catch { /* storage blocked */ }
}

function recallCode(): string {
  try {
    const saved = JSON.parse(localStorage.getItem(PENDING_KEY) ?? 'null');
    if (saved && typeof saved.code === 'string' && Date.now() - Number(saved.at) < PENDING_TTL_MS) return saved.code;
  } catch { /* storage blocked or corrupt */ }
  return '';
}

function forgetCode() {
  try { localStorage.removeItem(PENDING_KEY); } catch { /* storage blocked */ }
}

export function DeviceClient() {
  const proof = useRef('');
  const [phase, setPhase] = useState<Phase>('loading');
  const [code, setCode] = useState('');
  const [codeFromLink, setCodeFromLink] = useState(false);
  const [email, setEmail] = useState('');
  const [remember, setRemember] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    function reviewLink(): boolean {
      const token = new URLSearchParams(window.location.hash.slice(1)).get('token');
      if (!token) return false;
      proof.current = token;
      // Also handle an email opened in the tab that requested it (hash navigation).
      window.history.replaceState(null, '', window.location.pathname);
      const saved = recallCode();
      setBusy(true); setEmail(''); setError(''); setCode(saved); setCodeFromLink(Boolean(saved)); setPhase('review');
      void call('/auth/device/review', { token }).then(result => {
        if (proof.current === token) setEmail(typeof result.email === 'string' ? result.email : '');
      }).catch(() => {
        if (proof.current === token) setError('This link expired or is invalid. Run potluck login and request a new email.');
      }).finally(() => { if (proof.current === token) setBusy(false); });
      return true;
    }

    if (!reviewLink()) {
      const linked = new URLSearchParams(window.location.search).get('code') ?? '';
      if (linked) { setCode(displayCode(linked)); setCodeFromLink(true); }
      void call('/auth/device/session', undefined, 'GET').then(result => {
        if (result.signedIn === true && typeof result.email === 'string') {
          setEmail(result.email); setPhase('confirm');
        } else {
          setPhase('request');
        }
      }).catch(() => setPhase('request'));
    }
    const onHash = () => { reviewLink(); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  async function run(action: () => Promise<void>) {
    setBusy(true); setError('');
    try { await action(); }
    catch (err) {
      if (err instanceof RequestError && err.status === 401) { setEmail(''); setPhase('request'); }
      setError(err instanceof RequestError ? err.message : 'Could not connect. Try again.');
    }
    finally { setBusy(false); }
  }

  const decideAsBrowser = (approved: boolean) => run(async () => {
    const result = await call('/auth/device/approve-session', { user_code: code, approved });
    forgetCode();
    setPhase(result.status === 'approved' ? 'approved' : 'denied');
  });

  const decideByEmail = (approved: boolean) => run(async () => {
    const result = await call('/auth/device/approve', { token: proof.current, user_code: code, approved, remember });
    proof.current = '';
    forgetCode();
    setPhase(result.status === 'approved' ? 'approved' : 'denied');
  });

  const signOut = () => run(async () => {
    await call('/auth/logout');
    setEmail(''); setPhase('request');
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (phase === 'confirm') { void decideAsBrowser(true); return; }
    if (phase === 'review') { void decideByEmail(true); return; }
    void run(async () => {
      await call('/auth/device/request-link', { user_code: code, email });
      rememberCode(code);
      setPhase('sent');
    });
  }

  if (phase === 'loading') return <VerifyShell heading="Connect your terminal">
    <p className="text-sm text-muted-foreground">Checking this browser…</p>
  </VerifyShell>;

  if (phase === 'approved' || phase === 'denied') return <VerifyShell heading={phase === 'approved' ? 'Your terminal is connected' : 'Request declined'}>
    <p className="text-sm text-muted-foreground">{phase === 'approved' ? 'Return to your terminal to continue. You can close this tab.' : 'No session was granted. You can close this tab.'}</p>
  </VerifyShell>;

  if (phase === 'sent') return <VerifyShell heading="Check your email">
    <p className="text-sm text-muted-foreground">If your terminal code is valid, an approval link is on its way. Open it in this browser and the code will already be filled in.</p>
    <button className={buttonClass} onClick={() => setPhase('request')}>Change email or code</button>
  </VerifyShell>;

  const codeField = codeFromLink
    ? <div className="w-full rounded-lg border border-border px-4 py-3">
        <p className="text-xs text-muted-foreground">Code from the terminal</p>
        <p className="mt-1 font-mono text-2xl tracking-widest text-foreground" data-testid="device-code">{code}</p>
      </div>
    : <label className="text-sm text-foreground" htmlFor="device-code">Code from your terminal
        <input id="device-code" className={inputClass + ' font-mono uppercase tracking-widest'} value={code}
          onChange={event => setCode(event.target.value.toUpperCase())} placeholder="BCDF-GHJK" required
          minLength={8} maxLength={32} autoComplete="off" spellCheck={false} disabled={busy} />
      </label>;

  const warning = <p className="text-sm text-muted-foreground">
    Only approve if you just ran potluck login and this code matches your terminal. Approving signs that terminal in to your account.
  </p>;

  if (phase === 'confirm') return <VerifyShell heading="Approve this terminal">
    <p className="text-sm text-muted-foreground">Signed in as <strong className="font-medium text-foreground">{email}</strong>.</p>
    <form onSubmit={submit} className="flex w-full flex-col gap-4 text-left">
      {codeField}
      {warning}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <button className={buttonClass} type="submit" disabled={busy || code.length < 8}>{busy ? 'Connecting…' : 'Approve'}</button>
      <button className={quietClass} type="button" disabled={busy || code.length < 8} onClick={() => void decideAsBrowser(false)}>Decline</button>
    </form>
    <button className="text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline" type="button" disabled={busy} onClick={() => void signOut()}>
      Not {email}? Sign out of this browser
    </button>
  </VerifyShell>;

  return <VerifyShell heading={phase === 'review' ? 'Approve your terminal' : 'Connect your terminal'}>
    <p className="text-sm text-muted-foreground">{phase === 'review'
      ? `Sign your terminal in as ${email || '…'}.`
      : codeFromLink
        ? 'Enter your email and we will send you a link to approve this terminal.'
        : 'Run potluck login on your computer, then enter its code and your email here. You can use a different device for this step.'}</p>
    <form onSubmit={submit} className="flex w-full flex-col gap-4 text-left">
      {codeField}
      {phase === 'request' && <label className="text-sm text-foreground" htmlFor="device-email">Email
        <input id="device-email" className={inputClass} type="email" value={email} onChange={event => setEmail(event.target.value)} required autoComplete="email" disabled={busy} />
      </label>}
      {phase === 'review' && warning}
      {phase === 'review' && <label className="flex items-start gap-2 text-sm text-muted-foreground">
        <input type="checkbox" className="mt-1" checked={remember} onChange={event => setRemember(event.target.checked)} disabled={busy} />
        Keep this browser signed in, so approving your next terminal takes one click.
      </label>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <button className={buttonClass} type="submit" disabled={busy || (phase === 'review' && !email)}>{busy ? 'Connecting…' : phase === 'review' ? 'Approve sign-in' : 'Send approval email'}</button>
      {phase === 'review' && <button className={quietClass} type="button" disabled={busy || !email || code.length < 8} onClick={() => void decideByEmail(false)}>Decline</button>}
    </form>
  </VerifyShell>;
}
