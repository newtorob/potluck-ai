/**
 * Membership view — top-level route for /membership.
 *
 *   - Signed-out: editorial pitch + magic-link sign-in (MagicLinkForm ->
 *     VerifyingLink). The token itself returns via a potluck:// deep link
 *     handled globally by useDeepLinkAuth.
 *   - Signed-in: MembershipCard (plan, founding badge, account actions).
 */
import { useState } from 'react';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { PotluckLogo } from '@/components/brand/PotluckLogo';
import { MagicLinkForm } from '@/components/auth/MagicLinkForm';
import { VerifyingLink } from '@/components/auth/VerifyingLink';
import { MembershipCard } from '@/components/membership/MembershipCard';
import { useAuth } from '@/hooks/useAuth';

export function MembershipView() {
  const { isSignedIn, requestMagicLink } = useAuth();
  const [sentTo, setSentTo] = useState<string | null>(null);

  return (
    <div className="flex h-full flex-col">
      <ViewHeader
        eyebrow="The network"
        title="Membership"
        description="A founding membership in a member-operated network being built from the ground up."
      />

      <div className="relative flex flex-1 items-center justify-center p-8 grain">
        {isSignedIn ? (
          <MembershipCard />
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
                For people who'd rather own their AI than rent it.
              </h2>
              <p className="text-[15px] leading-relaxed text-muted-foreground">
                Potluck is member-operated. Founding members share the compute, earn credits for what they serve, and help steer where the network goes. Sign in to join before we announce publicly.
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
                  A voice in model curation and where the network goes
                </li>
                <li className="flex items-start gap-2.5">
                  <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-primary" aria-hidden="true" />
                  Access to the contributor network: share idle GPU compute, run larger models
                </li>
              </ul>
            </div>

            {/* Magic-link sign-in */}
            <div className="w-full">
              {sentTo === null ? (
                <MagicLinkForm onSent={setSentTo} />
              ) : (
                <VerifyingLink
                  email={sentTo}
                  onResend={() => {
                    void requestMagicLink(sentTo).catch(() => {
                      /* authError surfaced in the form/verify UI */
                    });
                  }}
                  onChangeEmail={() => setSentTo(null)}
                />
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
