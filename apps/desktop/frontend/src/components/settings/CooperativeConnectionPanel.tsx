/**
 * CooperativeConnectionPanel - coordinator URL configuration.
 *
 * Lives in Settings -> Advanced. The coordinator URL is the address of
 * the Phase G control plane (peer registry, mesh enrollment, blinded
 * inference dispatch, credit ledger).
 *
 * Source-of-truth precedence (matches connectionStore):
 *   1. Explicit user override (saved here) wins over everything.
 *   2. The mesh sidecar's reported coordinator_url from its live /status.
 *   3. VITE_COORDINATOR_URL build-time env.
 *   4. http://127.0.0.1:8400 default.
 *
 * When no explicit override is set, the field reflects whichever
 * coordinator the sidecar is actually dialing. If the user has an
 * override that differs from what the sidecar reports, a brief note
 * makes that visible so drift is never silent.
 *
 * Changes take effect immediately - the next API call uses the new URL,
 * and the coordinator-health probe re-runs.
 */

import { useEffect, useState } from 'react';
import { CheckCircle2, AlertCircle, Globe, Info } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { useConnectionStore } from '@/stores/connectionStore';
import { useCoordinatorHealth } from '@/hooks/useCoordinatorHealth';

export function CooperativeConnectionPanel() {
  const coordinatorUrl = useConnectionStore((s) => s.coordinatorUrl);
  const hasExplicitOverride = useConnectionStore((s) => s.hasExplicitOverride);
  const sidecarReportedUrl = useConnectionStore((s) => s.sidecarReportedUrl);
  const setCoordinatorUrl = useConnectionStore((s) => s.setCoordinatorUrl);
  const resetCoordinatorUrl = useConnectionStore((s) => s.resetCoordinatorUrl);

  const { reachable, lastCheckedAt } = useCoordinatorHealth();

  // Local draft so the user can type without thrashing localStorage and the
  // health probe on every keystroke. Committed on blur or Enter.
  const [draft, setDraft] = useState(coordinatorUrl);
  useEffect(() => {
    setDraft(coordinatorUrl);
  }, [coordinatorUrl]);

  const dirty = draft.trim().replace(/\/+$/, '') !== coordinatorUrl;

  // Drift: user has an explicit override that does not match what the sidecar
  // is actually dialing. Show a note so the mismatch is not silent.
  const hasDrift =
    hasExplicitOverride &&
    sidecarReportedUrl !== null &&
    sidecarReportedUrl !== coordinatorUrl;

  const commit = () => {
    const cleaned = draft.trim().replace(/\/+$/, '');
    if (cleaned.length === 0) return;
    setCoordinatorUrl(cleaned);
  };

  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <div className="mb-3 flex items-center gap-2">
        <Globe className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        <h3 className="text-sm font-semibold text-foreground">Network connection</h3>
      </div>

      <p className="mb-3 text-xs text-muted-foreground">
        The coordinator URL points to the Phase G control plane. When no override is
        set, the app follows whichever coordinator the mesh sidecar is actually
        connected to. Set an override to point at a coordinator on another machine
        in your fleet (LAN IP, Tailscale IP, or the network's hosted endpoint).
      </p>

      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <label
            htmlFor="coordinator-url"
            className="text-xs font-medium text-muted-foreground"
          >
            Coordinator URL
          </label>
          {!hasExplicitOverride && sidecarReportedUrl !== null && (
            <span className="text-[10px] text-muted-foreground/60">
              Following sidecar
            </span>
          )}
          {hasExplicitOverride && (
            <span className="text-[10px] text-muted-foreground/60">
              User override active
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <input
            id="coordinator-url"
            type="url"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.currentTarget.blur();
              } else if (e.key === 'Escape') {
                setDraft(coordinatorUrl);
                e.currentTarget.blur();
              }
            }}
            placeholder="http://127.0.0.1:8400"
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
            className={cn(
              'flex-1 rounded-md border bg-background px-3 py-1.5 text-sm font-mono',
              'border-border focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring',
              'placeholder:text-muted-foreground/50',
            )}
          />
          {hasExplicitOverride && (
            <button
              type="button"
              onClick={resetCoordinatorUrl}
              className={cn(
                'rounded-md border border-border px-2 py-1.5 text-xs font-medium',
                'text-muted-foreground hover:text-foreground hover:border-border',
                'focus:outline-none focus:ring-2 focus:ring-ring',
              )}
              title="Clear override and follow the sidecar's coordinator"
            >
              Reset
            </button>
          )}
        </div>

        {/* Drift warning: explicit override differs from what sidecar is dialing */}
        {hasDrift && (
          <div className="flex items-start gap-1.5 rounded-md bg-amber-500/10 px-2 py-1.5 text-xs text-amber-600 dark:text-amber-400">
            <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
            <span>
              Your override differs from the sidecar's active coordinator (
              <code className="rounded bg-muted px-1 py-0.5 font-mono text-[10px]">
                {sidecarReportedUrl}
              </code>
              ). The app is using your override. Reset to follow the sidecar.
            </span>
          </div>
        )}

        {/* Reachability indicator - hidden until the first probe settles */}
        <div className="flex items-center gap-1.5 text-xs">
          {lastCheckedAt === null ? (
            <span className="text-muted-foreground/50">Checking...</span>
          ) : reachable ? (
            <>
              <CheckCircle2 className="h-3 w-3 text-sage" aria-hidden="true" />
              <span className="text-muted-foreground">Reachable</span>
            </>
          ) : (
            <>
              <AlertCircle className="h-3 w-3 text-amber-500" aria-hidden="true" />
              <span className="text-muted-foreground">
                Unreachable. Verify the coordinator is running there (
                <code className="rounded bg-muted px-1 py-0.5 font-mono text-[10px]">make up</code>
                ).
              </span>
            </>
          )}
          {dirty && (
            <span className="ml-auto text-[11px] text-muted-foreground/70">
              Press Enter to apply
            </span>
          )}
        </div>
      </div>
    </section>
  );
}
