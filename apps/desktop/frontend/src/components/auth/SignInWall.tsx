/**
 * SignInWall - full-screen, blocking sign-in screen shown by AuthGate when
 * there is no signed-in session and no cached account to fall back to.
 *
 * Reuses MagicLinkForm/VerifyingLink, the same pair MembershipView uses for
 * its signed-out state, so the sign-in flow itself (and the potluck://
 * deep-link + SignInConfirmDialog handoff) is identical everywhere it
 * appears. This component owns only the framing and copy explaining why an
 * account is required here.
 */
import { useState } from 'react';
import { PotluckLogo } from '@/components/brand/PotluckLogo';
import { MagicLinkForm } from '@/components/auth/MagicLinkForm';
import { VerifyingLink } from '@/components/auth/VerifyingLink';
import { useAuth } from '@/hooks/useAuth';

export function SignInWall() {
  const { requestMagicLink } = useAuth();
  const [sentTo, setSentTo] = useState<string | null>(null);

  return (
    <div className="flex h-full w-full items-center justify-center bg-background p-8 grain">
      <div className="relative z-10 flex w-full max-w-md flex-col items-center gap-8 text-center animate-fade-in-up">
        <div className="relative h-16 w-16">
          <div
            className="absolute inset-0 rounded-2xl bg-primary/15 blur-xl"
            aria-hidden="true"
          />
          <div className="relative flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/12 ring-1 ring-primary/20">
            <PotluckLogo size={36} className="text-primary" aria-label="Potluck" />
          </div>
        </div>

        <div className="space-y-3">
          <h1 className="font-serif text-3xl leading-tight tracking-display text-foreground">
            Sign in to use Potluck
          </h1>
          <p className="text-[15px] leading-relaxed text-muted-foreground">
            Potluck runs on a network of member-owned machines, so every device needs an
            account tied to a member. Sign in with your email to continue. Your chats and
            models stay on this device either way.
          </p>
        </div>

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
    </div>
  );
}
