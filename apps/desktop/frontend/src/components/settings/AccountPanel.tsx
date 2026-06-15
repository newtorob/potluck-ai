/**
 * AccountPanel — placeholder for sign-in / membership features.
 *
 * Real auth (magic link) is implemented by Agent A. This panel shows
 * an informational card explaining that auth is coming and that local
 * chat is fully available without an account.
 */

import { User, Sparkles } from 'lucide-react';

export function AccountPanel() {
  return (
    <div className="space-y-4">
      {/* Main card */}
      <div className="rounded-xl border border-border bg-card p-6 space-y-4">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-primary/10">
            <User className="h-5 w-5 text-primary" />
          </div>
          <div>
            <p className="text-sm font-semibold text-foreground">Sign in coming soon</p>
            <p className="text-xs text-muted-foreground">Passwordless magic link, no password needed</p>
          </div>
        </div>

        <p className="text-sm text-muted-foreground leading-relaxed">
          Membership features (founding member status, governance voting, and the contributor
          network) are in active development. Sign-in is not required to use local chat. Your
          models run entirely on this device today.
        </p>

        <button
          type="button"
          disabled
          className="w-full rounded-lg border border-border bg-muted/50 px-4 py-2.5 text-sm font-medium text-muted-foreground cursor-not-allowed"
          aria-label="Sign in, coming soon"
        >
          Sign in with magic link
        </button>
      </div>

      {/* "What you get" info card */}
      <div className="rounded-xl border border-border bg-card p-5 space-y-3">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-primary" />
          <p className="text-sm font-medium text-foreground">Founding member perks</p>
        </div>
        <ul className="space-y-1.5 text-xs text-muted-foreground">
          <li className="flex items-start gap-2">
            <span className="mt-0.5 text-primary">·</span>
            Permanent founding-member badge and reduced dues
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 text-primary">·</span>
            A voice in model curation and where the network goes
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 text-primary">·</span>
            Access to the contributor network: share idle GPU compute, run larger models
          </li>
        </ul>
      </div>
    </div>
  );
}
