'use client';

import { useState, useId } from 'react';

type FormState =
  | { status: 'idle' }
  | { status: 'submitting' }
  | { status: 'success' }
  | { status: 'error'; message: string };

// Formspree endpoint, sourced from NEXT_PUBLIC_FORMSPREE_ENDPOINT so we can
// rotate the form ID without a code change. Free tier: 50 submissions/month.
// If the env var is unset (preview/dev), the form renders but submission
// returns a clear error rather than silently 404'ing.
const FORMSPREE_ENDPOINT = process.env.NEXT_PUBLIC_FORMSPREE_ENDPOINT ?? '';

export function WaitlistForm({ className }: { className?: string }) {
  const [state, setState] = useState<FormState>({ status: 'idle' });
  const emailId = useId();
  const nameId = useId();
  const errorId = useId();

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();

    if (!FORMSPREE_ENDPOINT) {
      setState({
        status: 'error',
        message: 'Waitlist signup is not configured yet. Please email hello@trypotluck.ai.',
      });
      return;
    }

    setState({ status: 'submitting' });

    const form = e.currentTarget;
    const data = new FormData(form);

    try {
      const response = await fetch(FORMSPREE_ENDPOINT, {
        method: 'POST',
        body: data,
        headers: { Accept: 'application/json' },
      });

      if (response.ok) {
        setState({ status: 'success' });
        form.reset();
      } else {
        const json = (await response.json()) as { error?: string };
        setState({
          status: 'error',
          message: json.error ?? 'Something went wrong. Try again or email us directly.',
        });
      }
    } catch {
      setState({
        status: 'error',
        message: 'Could not reach the server. Check your connection and try again.',
      });
    }
  }

  if (state.status === 'success') {
    return (
      <div
        className={[
          'rounded-lg border border-sage/40 bg-sage/10 px-6 py-5 text-center',
          className,
        ]
          .filter(Boolean)
          .join(' ')}
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        <p className="font-medium text-foreground">You&apos;re on the list.</p>
        <p className="mt-1 text-sm text-muted-foreground">
          We&apos;ll reach out when we open early access. No spam.
        </p>
      </div>
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className={['flex flex-col gap-3', className].filter(Boolean).join(' ')}
      aria-label="Waitlist signup"
      noValidate
    >
      <div className="flex flex-col gap-1.5">
        <label htmlFor={nameId} className="text-sm text-muted-foreground">
          Name <span className="opacity-60">(optional)</span>
        </label>
        <input
          id={nameId}
          name="name"
          type="text"
          placeholder="Ada Lovelace"
          autoComplete="name"
          className="w-full rounded-sm border border-border bg-surface-2 px-3.5 py-2.5 text-sm font-sans text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1 focus:ring-offset-background transition-shadow duration-150"
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor={emailId} className="text-sm text-muted-foreground">
          Email{' '}
          <span aria-hidden="true" className="text-primary">*</span>
          <span className="sr-only">(required)</span>
        </label>
        <input
          id={emailId}
          name="email"
          type="email"
          required
          placeholder="ada@example.com"
          autoComplete="email"
          aria-required="true"
          aria-describedby={state.status === 'error' ? errorId : undefined}
          aria-invalid={state.status === 'error' ? 'true' : undefined}
          className="w-full rounded-sm border border-border bg-surface-2 px-3.5 py-2.5 text-sm font-sans text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1 focus:ring-offset-background transition-shadow duration-150 aria-invalid:border-destructive"
        />
      </div>

      {/* Honeypot — spam prevention */}
      <input name="_gotcha" type="text" className="hidden" aria-hidden="true" tabIndex={-1} />

      {state.status === 'error' && (
        <p
          id={errorId}
          className="text-sm text-destructive"
          role="alert"
          aria-live="assertive"
          aria-atomic="true"
        >
          {state.message}
        </p>
      )}

      <button
        type="submit"
        disabled={state.status === 'submitting'}
        aria-disabled={state.status === 'submitting'}
        aria-label={state.status === 'submitting' ? 'Sending your request…' : 'Request early access'}
        className="mt-1 btn-primary w-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:opacity-60 disabled:cursor-not-allowed"
      >
        {state.status === 'submitting' ? 'Sending…' : 'Request early access'}
      </button>

      <p className="text-xs text-muted-foreground/70 text-center">
        No spam. No selling your email. We build in the open.
      </p>
    </form>
  );
}
