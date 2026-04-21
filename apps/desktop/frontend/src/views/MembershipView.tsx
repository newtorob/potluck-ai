import { CreditCard, LogIn } from 'lucide-react';

/**
 * Membership view — top-level route for /membership.
 *
 * Placeholder scaffold. Real implementation:
 *   - Signed-out state: email input -> magic link -> verifying screen
 *   - Signed-in state: MembershipCard (plan, founding badge), billing portal link
 *   - Governance: active proposals, voting (guide §5 / cloudApi.getProposals)
 */
export function MembershipView() {
  // TODO: read from authStore — replace with real auth state
  const isSignedIn = false;

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-border px-6 py-4">
        <CreditCard className="h-5 w-5 text-muted-foreground" />
        <h1 className="text-sm font-semibold">Membership</h1>
      </div>

      <div className="flex flex-1 items-center justify-center p-8">
        {isSignedIn ? (
          /* Founding member card placeholder */
          <div className="w-full max-w-sm rounded-2xl border border-border bg-card p-6 space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-sm font-semibold text-foreground">Founding Member</span>
              <span className="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary">
                Active
              </span>
            </div>
            <div className="space-y-2">
              <div className="h-3 w-3/4 animate-pulse rounded bg-muted" />
              <div className="h-3 w-1/2 animate-pulse rounded bg-muted" />
            </div>
            <div className="pt-2">
              <div className="h-9 w-full animate-pulse rounded-lg bg-muted" />
            </div>
          </div>
        ) : (
          /* Sign-in state */
          <div className="flex flex-col items-center gap-4 text-center">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10">
              <LogIn className="h-7 w-7 text-primary" />
            </div>
            <div className="space-y-1">
              <p className="text-base font-medium text-foreground">Sign in to Potluck AI</p>
              <p className="text-sm text-muted-foreground">
                Passwordless magic link — your email, nothing else.
              </p>
            </div>
            {/* Email input placeholder — MagicLinkForm component goes here */}
            <div className="w-full max-w-xs space-y-3">
              <div className="h-10 w-full animate-pulse rounded-lg bg-muted" />
              <div className="h-10 w-full animate-pulse rounded-lg bg-muted" />
            </div>
            <p className="text-xs text-muted-foreground/60">
              Real form: MagicLinkForm + VerifyingLink — guide §4.7
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
