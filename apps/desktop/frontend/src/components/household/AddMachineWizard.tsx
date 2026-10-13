/**
 * AddMachineWizard - modal dialog for issuing a pairing token from an
 * already-enrolled machine.
 *
 * Flow:
 *   1. Open dialog → call coordinator POST /mesh/pairing-tokens.
 *   2. Display the token + copy-to-clipboard + countdown to expiry.
 *   3. User walks to the other machine, opens Potluck, pastes token in
 *      the "join an existing household" panel of /household.
 *
 * Out of scope for v0 of this wizard (follow-ups):
 *   - QR rendering. Token paste-from-clipboard is the dominant flow when
 *     both machines are desktops. QR is nice when one is a phone - skip
 *     until we have a phone client.
 *   - Live "peer just joined" celebration. The HouseholdView's roster
 *     refresh picks up the new peer within 5s of pairing on the other
 *     side.
 */

import { useEffect, useRef, useState } from 'react';
import { Copy, Check, X, Plus, AlertCircle, Clock, RadioTower } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { issuePairingToken, meshClient } from '@/lib/meshClient';
import { useConnectionStore } from '@/stores/connectionStore';
import { describeCoordinatorAuthError } from '@/lib/coordinatorErrorMessage';
import type { PairingTokenIssueResponse } from '@/types/mesh';

interface AddMachineWizardProps {
  open: boolean;
  onClose: () => void;
}

export function AddMachineWizard({ open, onClose }: AddMachineWizardProps) {
  const [token, setToken] = useState<PairingTokenIssueResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [switchToUrl, setSwitchToUrl] = useState<string | null>(null);
  const [isIssuing, setIsIssuing] = useState(false);
  const [copied, setCopied] = useState(false);
  const [broadcasting, setBroadcasting] = useState(false);
  const [confirmationPin, setConfirmationPin] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  // Bumped by "Switch and try again" to re-run the issue-token effect below
  // without needing to extract it into a standalone callback.
  const [retryNonce, setRetryNonce] = useState(0);

  const dialogRef = useRef<HTMLDivElement>(null);
  const copyBtnRef = useRef<HTMLButtonElement>(null);

  // Issue a token whenever the dialog opens (or a "Switch and try again"
  // bumps retryNonce while it's already open).
  useEffect(() => {
    if (!open) return;

    let cancelled = false;
    setToken(null);
    setError(null);
    setSwitchToUrl(null);
    setCopied(false);
    setBroadcasting(false);
    setConfirmationPin(null);
    setIsIssuing(true);

    void (async () => {
      try {
        const res = await issuePairingToken();
        if (!cancelled) setToken(res);
        // Best-effort: also push a fingerprint of the token onto the local
        // network so a machine on the same LAN can discover it without
        // copy-paste. The raw token never goes on the wire - the sidecar
        // returns a confirmation_pin instead, which we display here for the
        // user to read aloud to whoever is joining. Failure (no mesh
        // sidecar, no LAN) just falls back to manual paste.
        if (!cancelled) {
          void meshClient
            .broadcastPairingToken(res.token)
            .then((broadcastRes) => {
              if (!cancelled) {
                setBroadcasting(true);
                setConfirmationPin(broadcastRes.confirmation_pin);
              }
            })
            .catch(() => {
              /* LAN broadcast unavailable; manual token still works */
            });
        }
      } catch (err) {
        if (cancelled) return;
        // Covers a stale coordinator override the same way BeFirstPanel's
        // "Set up household" does - see describeCoordinatorAuthError.
        const info = describeCoordinatorAuthError(err, {
          coordinatorUrl: useConnectionStore.getState().coordinatorUrl,
          sidecarReportedUrl: useConnectionStore.getState().sidecarReportedUrl,
        });
        setError(info.message);
        setSwitchToUrl(info.switchToUrl ?? null);
      } finally {
        if (!cancelled) setIsIssuing(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, retryNonce]);

  // Tick once per second for the countdown.
  useEffect(() => {
    if (!open || token === null) return;
    const interval = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(interval);
  }, [open, token]);

  // Esc-to-close + initial focus.
  useEffect(() => {
    if (!open) return;
    function handler(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handler);
    // Defer focus to next tick so the dialog has actually mounted.
    setTimeout(() => copyBtnRef.current?.focus(), 0);
    return () => document.removeEventListener('keydown', handler);
  }, [open, onClose]);

  // Reset copy state after 2s of "Copied!" feedback.
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 2_000);
    return () => clearTimeout(t);
  }, [copied]);

  if (!open) return null;

  async function handleCopy() {
    if (token === null) return;
    try {
      await navigator.clipboard.writeText(token.token);
      setCopied(true);
    } catch (err) {
      setError(
        err instanceof Error
          ? `Could not copy to clipboard: ${err.message}`
          : 'Could not copy to clipboard.',
      );
    }
  }

  function handleSwitchAndRetry() {
    if (switchToUrl === null) return;
    useConnectionStore.getState().setCoordinatorUrl(switchToUrl);
    setRetryNonce((n) => n + 1);
  }

  const expiresInMs = token ? new Date(token.expires_at).getTime() - now : 0;
  const minutesLeft = Math.max(0, Math.floor(expiresInMs / 60_000));
  const isExpired = token !== null && expiresInMs <= 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-4 backdrop-blur-sm animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby="add-machine-title"
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        className={cn(
          'relative w-full max-w-lg overflow-hidden rounded-2xl border border-border bg-card shadow-hearth-lg',
          'animate-fade-in-up',
        )}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Close button */}
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className={cn(
            'absolute right-3 top-3 z-10 flex h-7 w-7 items-center justify-center rounded-md',
            'text-muted-foreground hover:text-foreground hover:bg-accent transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          <X className="h-4 w-4" />
        </button>

        {/* Header */}
        <div className="border-b border-border/60 px-6 pb-4 pt-5">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/12 ring-1 ring-primary/20">
              <Plus className="h-4 w-4 text-primary" aria-hidden="true" />
            </div>
            <div>
              <h2
                id="add-machine-title"
                className="font-serif text-lg leading-tight tracking-display text-foreground"
              >
                Add another machine
              </h2>
              <p className="text-xs text-muted-foreground">
                One-time token, expires in an hour.
              </p>
            </div>
          </div>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-5">
          {isIssuing && (
            <div className="flex items-center gap-3 rounded-xl border border-border/60 bg-muted/30 p-4">
              <div
                className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent"
                role="status"
                aria-label="Issuing token"
              />
              <span className="text-sm text-muted-foreground">
                Issuing pairing token…
              </span>
            </div>
          )}

          {error !== null && (
            <div
              className="flex items-start gap-2.5 rounded-xl border border-destructive/30 bg-destructive/8 p-4"
              role="alert"
            >
              <AlertCircle
                className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
                aria-hidden="true"
              />
              <div className="space-y-1.5 text-sm">
                <p className="font-medium text-foreground">Couldn't issue token</p>
                <p className="text-muted-foreground">{error}</p>
                {switchToUrl !== null && (
                  <button
                    type="button"
                    onClick={handleSwitchAndRetry}
                    className={cn(
                      'inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium',
                      'bg-primary text-primary-foreground hover:bg-primary/90 transition-colors',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    )}
                  >
                    Switch and try again
                  </button>
                )}
              </div>
            </div>
          )}

          {token !== null && !error && (
            <>
              {/* Token block */}
              <div className="space-y-2">
                <p className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
                  Pairing token
                </p>
                <div className="flex items-stretch gap-2">
                  <code
                    className={cn(
                      'flex-1 select-all rounded-lg border border-border bg-muted/40 px-3 py-2.5',
                      'font-mono text-sm text-foreground break-all leading-snug',
                    )}
                  >
                    {token.token}
                  </code>
                  <button
                    ref={copyBtnRef}
                    type="button"
                    onClick={() => void handleCopy()}
                    disabled={isExpired}
                    aria-label={copied ? 'Copied' : 'Copy token to clipboard'}
                    className={cn(
                      'inline-flex shrink-0 items-center justify-center gap-1.5 rounded-lg px-3 py-2',
                      'border text-sm font-medium transition-colors',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                      copied
                        ? 'border-sage/30 bg-sage/12 text-sage'
                        : 'border-primary/30 bg-primary/10 text-primary hover:bg-primary/15',
                      isExpired && 'cursor-not-allowed opacity-50',
                    )}
                  >
                    {copied ? (
                      <>
                        <Check className="h-3.5 w-3.5" />
                        Copied
                      </>
                    ) : (
                      <>
                        <Copy className="h-3.5 w-3.5" />
                        Copy
                      </>
                    )}
                  </button>
                </div>

                {/* Countdown */}
                <p
                  className={cn(
                    'flex items-center gap-1.5 text-xs',
                    isExpired
                      ? 'text-destructive'
                      : minutesLeft < 5
                        ? 'text-amber-600 dark:text-amber-400'
                        : 'text-muted-foreground',
                  )}
                >
                  <Clock className="h-3 w-3" aria-hidden="true" />
                  {isExpired
                    ? 'Token expired. Close this dialog and issue a new one.'
                    : minutesLeft === 0
                      ? 'Expires in under a minute.'
                      : `Expires in ${minutesLeft} minute${minutesLeft === 1 ? '' : 's'}.`}
                </p>
              </div>

              {/* Instructions */}
              <ol className="space-y-2.5 text-sm text-muted-foreground">
                <Step n={1}>
                  Open Potluck on the other machine. (Install it first if you
                  haven't yet.)
                </Step>
                <Step n={2}>
                  Go to <span className="font-medium text-foreground">My Machines</span>, then{' '}
                  <span className="font-medium text-foreground">"Join existing household"</span>.
                </Step>
                <Step n={3}>
                  Paste this token, or if the other machine is on this network,
                  pick this invite from its "Join existing household" panel and
                  enter the PIN shown below when broadcasting starts. The two
                  machines then connect directly on the LAN or relay through
                  Potluck's DERP servers if they can't reach each other.
                </Step>
              </ol>

              {broadcasting && confirmationPin !== null && (
                <div className="space-y-2 rounded-xl border border-sage/30 bg-sage/8 p-4">
                  <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <RadioTower className="h-3 w-3 text-sage" aria-hidden="true" />
                    Also inviting machines on this network for 2 minutes. On
                    the other device, pick this invite and enter this PIN:
                  </p>
                  <p
                    className="text-center font-mono text-2xl font-semibold tracking-[0.3em] text-foreground"
                    aria-label={`Confirmation PIN ${confirmationPin.split('').join(' ')}`}
                  >
                    {confirmationPin}
                  </p>
                  <p className="text-center text-[11px] text-muted-foreground/70">
                    This PIN is never sent over the network. Read it aloud or
                    show this screen to whoever is joining.
                  </p>
                </div>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-border/60 bg-muted/20 px-6 py-3">
          <p className="text-[11px] text-muted-foreground/70">
            Tokens are single-use. A leaked token can only be redeemed once,
            within the hour.
          </p>
          <button
            type="button"
            onClick={onClose}
            className={cn(
              'rounded-lg border border-border/60 bg-card px-3 py-1.5 text-xs font-medium text-foreground',
              'hover:bg-accent transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Numbered step row
// ---------------------------------------------------------------------------

function Step({ n, children }: { n: number; children: React.ReactNode }) {
  return (
    <li className="flex items-start gap-2.5">
      <span
        className={cn(
          'mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full',
          'bg-muted text-[11px] font-medium text-muted-foreground tabular-nums',
        )}
        aria-hidden="true"
      >
        {n}
      </span>
      <span className="leading-snug">{children}</span>
    </li>
  );
}
