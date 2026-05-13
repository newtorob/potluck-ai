/**
 * Membership view — top-level route for /membership.
 *
 * Placeholder scaffold. Real implementation:
 *   - Signed-out state: email input -> magic link -> verifying screen
 *   - Signed-in state: MembershipCard (plan, founding badge), billing portal link
 *   - Governance: active proposals, voting (guide §5 / cloudApi.getProposals)
 */
import { ViewHeader } from '@/components/layout/ViewHeader';
import { PotluckLogo } from '@/components/brand/PotluckLogo';

export function MembershipView() {
  // TODO: read from authStore — replace with real auth state
  const isSignedIn = false;

  return (
    <div className="flex h-full flex-col">
      <ViewHeader
        eyebrow="Cooperative"
        title="Membership"
        description="A founding membership in a cooperative being built from the ground up."
      />

      <div className="relative flex flex-1 items-center justify-center p-8 grain">
        {isSignedIn ? (
          /* Founding member card placeholder */
          <div className="relative z-10 w-full max-w-md rounded-2xl border border-border/60 bg-card p-8 space-y-6 shadow-hearth-sm animate-fade-in-up">
            <div className="space-y-1">
              <h2 className="font-serif text-3xl leading-tight tracking-display text-foreground">Founding Member</h2>
              <p className="text-sm text-muted-foreground">You're part of something being built from the ground up.</p>
            </div>
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center gap-1.5 rounded-full border border-sage/25 bg-sage/12 px-3 py-1 text-xs font-medium text-sage">
                <span className="h-1.5 w-1.5 rounded-full bg-sage animate-hearth-pulse" />
                Active
              </span>
            </div>
            <div className="space-y-2">
              <div className="h-3 w-3/4 animate-hearth-pulse rounded bg-muted" />
              <div className="h-3 w-1/2 animate-hearth-pulse rounded bg-muted/70" />
            </div>
            <div className="pt-2">
              <div className="h-9 w-full animate-hearth-pulse rounded-xl bg-muted" />
            </div>
          </div>
        ) : (
          /* Sign-in state — editorial treatment */
          <div className="relative z-10 flex w-full max-w-md flex-col items-center gap-10 text-center animate-fade-in-up">
            {/* Logo mark with ember halo */}
            <div className="relative h-16 w-16">
              <div
                className="absolute inset-0 rounded-2xl bg-primary/15 blur-xl"
                aria-hidden="true"
              />
              <div className="relative flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/12 ring-1 ring-primary/20">
                <PotluckLogo size={36} className="text-primary" aria-label="Potluck" />
              </div>
            </div>

            {/* Editorial heading block */}
            <div className="space-y-4">
              <h2 className="font-serif text-3xl leading-tight tracking-display text-foreground">
                A cooperative for people who think AI should be owned differently.
              </h2>
              <p className="text-[15px] leading-relaxed text-muted-foreground">
                Potluck is member-owned. Founding members set the direction, share the compute, and hold the governance rights. Sign in to join before we announce publicly.
              </p>
            </div>

            {/* What you get */}
            <div className="w-full rounded-xl border border-border/60 bg-card/60 px-6 py-5 text-left space-y-3 shadow-hearth-sm">
              <p className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">Founding member perks</p>
              <ul className="space-y-2 text-sm text-muted-foreground">
                <li className="flex items-start gap-2.5">
                  <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-primary" aria-hidden="true" />
                  Permanent founding-member badge and reduced dues
                </li>
                <li className="flex items-start gap-2.5">
                  <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-primary" aria-hidden="true" />
                  Governance voting rights on model curation and cooperative direction
                </li>
                <li className="flex items-start gap-2.5">
                  <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-primary" aria-hidden="true" />
                  Access to the contributor network — share idle GPU compute, run larger models
                </li>
              </ul>
            </div>

            {/* Sign-in form placeholder */}
            <div className="w-full space-y-3">
              <div className="h-11 w-full animate-hearth-pulse rounded-xl bg-muted" />
              <div className="h-11 w-full animate-hearth-pulse rounded-xl bg-primary/20" />
              <p className="text-xs italic text-muted-foreground/60">
                Passwordless magic link — your email, nothing else.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
