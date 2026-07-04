/**
 * MembershipCard — signed-in state: identity, plan, founding badge, and the
 * account actions. With an active plan it offers "Manage billing" (Stripe
 * portal); without one it offers "Become a founding member" (Stripe checkout).
 * Both open Stripe in the external browser. Until a live Stripe key is set the
 * cloud API returns 503, which surfaces here as an honest "not available yet".
 */

import { useState } from 'react';
import { LogOut } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth';
import { useMembership } from '@/hooks/useMembership';

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function MembershipCard() {
  const { memberProfile, signOut } = useAuth();
  const { membership, openBillingPortal, startCheckout } = useMembership();
  const [signingOut, setSigningOut] = useState(false);
  const [billingBusy, setBillingBusy] = useState(false);
  const [billingError, setBillingError] = useState<string | null>(null);

  const plan = membership?.plan ?? null;
  const isFounding = membership?.isFoundingMember ?? memberProfile?.isFoundingMember ?? false;
  const status = membership?.status ?? 'none';
  const hasActivePlan = plan !== null && status !== 'none';
  const planLabel = isFounding
    ? 'Founding Member'
    : plan
      ? `${titleCase(plan)} Member`
      : 'Member';

  async function handleBilling() {
    setBillingError(null);
    setBillingBusy(true);
    try {
      await openBillingPortal();
    } catch {
      setBillingError("Billing isn't available yet.");
    } finally {
      setBillingBusy(false);
    }
  }

  async function handleCheckout() {
    setBillingError(null);
    setBillingBusy(true);
    try {
      await startCheckout('founding');
    } catch {
      setBillingError("Billing isn't available yet.");
    } finally {
      setBillingBusy(false);
    }
  }

  async function handleSignOut() {
    setSigningOut(true);
    try {
      await signOut();
    } finally {
      setSigningOut(false);
    }
  }

  return (
    <div className="relative z-10 w-full max-w-md space-y-6 rounded-2xl border border-border/60 bg-card p-8 shadow-hearth-sm animate-fade-in-up">
      <div className="space-y-1">
        <h2 className="font-serif text-3xl leading-tight tracking-display text-foreground">
          {planLabel}
        </h2>
        <p className="text-sm text-muted-foreground">
          {memberProfile?.email ?? 'Signed in'}
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {status === 'active' ? (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-sage/25 bg-sage/12 px-3 py-1 text-xs font-medium text-sage">
            <span className="h-1.5 w-1.5 rounded-full bg-sage animate-hearth-pulse" />
            Active
          </span>
        ) : (
          <span className="inline-flex items-center rounded-full border border-border/60 bg-muted/40 px-3 py-1 text-xs font-medium text-muted-foreground">
            {hasActivePlan ? titleCase(status) : 'Signed in'}
          </span>
        )}
        {isFounding && (
          <span className="inline-flex items-center rounded-full border border-primary/25 bg-primary/12 px-3 py-1 text-xs font-medium text-primary">
            Founding badge
          </span>
        )}
      </div>

      {hasActivePlan ? (
        <div className="space-y-2">
          <button
            type="button"
            onClick={() => void handleBilling()}
            disabled={billingBusy}
            className="h-10 w-full rounded-xl border border-border bg-card text-sm font-medium text-foreground transition-colors hover:bg-muted/50 disabled:opacity-50"
          >
            {billingBusy ? 'Opening…' : 'Manage billing'}
          </button>
          {billingError && (
            <p role="alert" className="text-xs text-destructive">
              {billingError}
            </p>
          )}
        </div>
      ) : (
        <div className="space-y-2">
          <button
            type="button"
            onClick={() => void handleCheckout()}
            disabled={billingBusy}
            className="h-11 w-full rounded-xl bg-primary text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {billingBusy ? 'Opening…' : 'Become a founding member'}
          </button>
          {billingError && (
            <p role="alert" className="text-xs text-destructive">
              {billingError}
            </p>
          )}
          <p className="text-sm leading-relaxed text-muted-foreground">
            A one-time founding fee locks in the permanent badge and reduced
            dues. Your account is ready; nothing is charged until you choose to
            join.
          </p>
        </div>
      )}

      <div className="border-t border-border/60 pt-4">
        <button
          type="button"
          onClick={() => void handleSignOut()}
          disabled={signingOut}
          className="inline-flex items-center gap-2 text-xs text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
        >
          <LogOut className="h-3.5 w-3.5" />
          {signingOut ? 'Signing out…' : 'Sign out'}
        </button>
      </div>
    </div>
  );
}
