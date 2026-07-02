/**
 * MagicLinkForm — email input for passwordless sign-in.
 *
 * On submit, requests a magic link and calls onSent so the parent can switch to
 * the "check your email" state. Errors are shown inline. The token itself
 * arrives later via a potluck:// deep link (see useDeepLinkAuth).
 */

import { useState, type FormEvent } from 'react';
import { useAuth } from '@/hooks/useAuth';

interface MagicLinkFormProps {
  onSent: (email: string) => void;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function MagicLinkForm({ onSent }: MagicLinkFormProps) {
  const { requestMagicLink, authError } = useAuth();
  const [email, setEmail] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const valid = EMAIL_RE.test(email.trim());

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!valid || submitting) return;
    setSubmitting(true);
    try {
      await requestMagicLink(email.trim());
      onSent(email.trim());
    } catch {
      // authError is set by the hook and rendered below.
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={(e) => void handleSubmit(e)} className="w-full space-y-3" noValidate>
      <input
        type="email"
        inputMode="email"
        autoComplete="email"
        autoFocus
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="you@example.com"
        aria-label="Email address"
        aria-invalid={authError ? true : undefined}
        className="h-11 w-full rounded-xl border border-border bg-card px-4 text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-primary/40"
      />
      <button
        type="submit"
        disabled={!valid || submitting}
        className="h-11 w-full rounded-xl bg-primary text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {submitting ? 'Sending…' : 'Send magic link'}
      </button>
      {authError && (
        <p role="alert" className="text-xs text-destructive">
          {authError}
        </p>
      )}
      <p className="text-xs italic text-muted-foreground/60">
        Passwordless magic link. Your email, nothing else.
      </p>
    </form>
  );
}
