/**
 * AccountPanel - Settings → Account. Reflects real auth state:
 *   - Signed in: identity + founding badge + link to Membership + sign out.
 *   - Signed out: a prompt to sign in (local chat needs no account).
 *
 * The full sign-in flow lives on the Membership view; this panel routes there.
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { User, Sparkles, LogOut, RefreshCw } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth';
import { ipc } from '@/lib/ipc';
import { useUpdateStore } from '@/stores/updateStore';

export function AccountPanel() {
  const navigate = useNavigate();
  const { isSignedIn, memberProfile, signOut } = useAuth();
  const [signingOut, setSigningOut] = useState(false);

  // ── Version + update status ────────────────────────────────────────────
  const buildInfo = ipc.buildInfo;
  const updatePhase = useUpdateStore((s) => s.phase);
  const updateVersion = useUpdateStore((s) => s.version);
  const updatePercent = useUpdateStore((s) => s.percent);
  const updateStatusText =
    updatePhase === 'downloading'
      ? `Downloading update… ${updatePercent}%`
      : updatePhase === 'ready'
        ? 'Update ready, restart to install'
        : updatePhase === 'available'
          ? `Update available${updateVersion ? ` (v${updateVersion})` : ''}`
          : updatePhase === 'error'
            ? 'Update check failed'
            : 'Up to date';

  async function handleSignOut() {
    setSigningOut(true);
    try {
      await signOut();
    } finally {
      setSigningOut(false);
    }
  }

  return (
    <div className="space-y-4">
      {/* Main card */}
      <div className="rounded-xl border border-border bg-card p-6 space-y-4">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-primary/10">
            <User className="h-5 w-5 text-primary" />
          </div>
          <div className="min-w-0">
            {isSignedIn ? (
              <>
                <p className="truncate text-sm font-semibold text-foreground">
                  {memberProfile?.email ?? 'Signed in'}
                </p>
                <p className="text-xs text-muted-foreground">
                  {memberProfile?.isFoundingMember ? 'Founding member' : 'Member'}
                </p>
              </>
            ) : (
              <>
                <p className="text-sm font-semibold text-foreground">Not signed in</p>
                <p className="text-xs text-muted-foreground">Passwordless magic link, no password needed</p>
              </>
            )}
          </div>
        </div>

        <p className="text-sm text-muted-foreground leading-relaxed">
          Membership features (founding member status, governance voting, and the contributor
          network) live on the Membership page. Sign-in is not required to use local chat. Your
          models run entirely on this device.
        </p>

        {isSignedIn ? (
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => navigate('/membership')}
              className="rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              Manage membership
            </button>
            <button
              type="button"
              onClick={() => void handleSignOut()}
              disabled={signingOut}
              className="inline-flex items-center gap-2 rounded-lg border border-border px-4 py-2.5 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
            >
              <LogOut className="h-4 w-4" />
              {signingOut ? 'Signing out…' : 'Sign out'}
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => navigate('/membership')}
            className="w-full rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
          >
            Sign in with magic link
          </button>
        )}
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

      {/* Version + update status */}
      <div className="rounded-xl border border-border bg-card p-5">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm text-foreground">
              v{buildInfo?.version ?? ipc.runningVersion}
              {buildInfo?.sha && (
                <span className="ml-1.5 font-mono text-xs text-muted-foreground">
                  {buildInfo.sha.slice(0, 7)}
                </span>
              )}
            </p>
            <p className="text-xs text-muted-foreground">{updateStatusText}</p>
          </div>
          <button
            type="button"
            onClick={() => useUpdateStore.getState().check()}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Check for updates
          </button>
        </div>
      </div>
    </div>
  );
}
