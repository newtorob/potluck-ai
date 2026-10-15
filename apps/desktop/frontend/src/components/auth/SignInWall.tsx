import { useState } from 'react';
import { PotluckLogo } from '@/components/brand/PotluckLogo';
import { MagicLinkForm } from '@/components/auth/MagicLinkForm';
import { VerifyingLink } from '@/components/auth/VerifyingLink';
import { useAuth } from '@/hooks/useAuth';

export function SignInWall({ onContinueLocal, checkingAccount = false }: {
  onContinueLocal?: () => void;
  checkingAccount?: boolean;
}) {
  const { requestMagicLink } = useAuth();
  const [sentTo, setSentTo] = useState<string | null>(null);

  return (
    <div className="flex h-full w-full overflow-y-auto bg-background px-6 py-8 grain">
      <div className="relative z-10 m-auto flex w-full max-w-md flex-col items-center gap-8 text-center animate-fade-in-up">
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
            Start with Potluck
          </h1>
          <p className="text-[15px] leading-relaxed text-muted-foreground">
            Chat with a model on this machine and manage your models without an account.
            Sign in to connect your machines or join a trusted circle.
          </p>
        </div>

        {onContinueLocal && (
          <div className="w-full space-y-2">
            <button type="button" autoFocus onClick={onContinueLocal}
              className="w-full rounded-lg bg-primary px-4 py-3 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2">
              Use on this machine
            </button>
            <p className="text-xs leading-relaxed text-muted-foreground">You can sign in later. Your local chats and settings will still be here.</p>
          </div>
        )}

        <div className="w-full space-y-4">
          <p className="text-sm font-medium text-foreground">Or sign in to connect</p>
          {checkingAccount && <p role="status" className="text-xs text-muted-foreground">Checking your account… You can continue locally now.</p>}
          {sentTo === null ? (
            <MagicLinkForm onSent={setSentTo} autoFocus={!onContinueLocal} />
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
