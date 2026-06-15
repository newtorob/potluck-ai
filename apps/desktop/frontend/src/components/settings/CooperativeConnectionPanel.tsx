/**
 * CooperativeConnectionPanel — runtime-editable coordinator URL.
 *
 * Lives in Settings → Advanced. The coordinator URL is the address of
 * the Phase G control plane (peer registry, mesh enrollment, blinded
 * inference dispatch, credit ledger). Defaults to the docker-compose
 * binding at `http://127.0.0.1:8400`; editable so members can point at
 * a coordinator on another machine in their fleet (LAN, Tailscale,
 * hosted) without rebuilding the app.
 *
 * Persists to localStorage via `useConnectionStore`. Changes take
 * effect immediately — the next API call uses the new URL, and the
 * coordinator-health probe re-runs.
 */

import { useEffect, useState } from 'react';
import { CheckCircle2, AlertCircle, Globe } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { useConnectionStore } from '@/stores/connectionStore';
import { useCoordinatorHealth } from '@/hooks/useCoordinatorHealth';

const DEFAULT_URL = 'http://127.0.0.1:8400';

export function CooperativeConnectionPanel() {
  const coordinatorUrl = useConnectionStore((s) => s.coordinatorUrl);
  const setCoordinatorUrl = useConnectionStore((s) => s.setCoordinatorUrl);
  const resetCoordinatorUrl = useConnectionStore((s) => s.resetCoordinatorUrl);

  const { reachable, lastCheckedAt } = useCoordinatorHealth();

  // Local draft state so the user can type without thrashing localStorage
  // + the health probe on every keystroke. Commit on blur or Enter.
  const [draft, setDraft] = useState(coordinatorUrl);
  useEffect(() => {
    setDraft(coordinatorUrl);
  }, [coordinatorUrl]);

  const dirty = draft.trim().replace(/\/+$/, '') !== coordinatorUrl;
  const isDefault = coordinatorUrl === DEFAULT_URL;

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
        The coordinator URL points to the Phase G control plane. Default is the local
        docker-compose binding. Change this to point at a coordinator on another machine
        in your fleet (LAN IP, Tailscale IP, or the network's hosted endpoint).
      </p>

      <div className="flex flex-col gap-2">
        <label
          htmlFor="coordinator-url"
          className="text-xs font-medium text-muted-foreground"
        >
          Coordinator URL
        </label>
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
          {!isDefault && (
            <button
              type="button"
              onClick={resetCoordinatorUrl}
              className={cn(
                'rounded-md border border-border px-2 py-1.5 text-xs font-medium',
                'text-muted-foreground hover:text-foreground hover:border-border',
                'focus:outline-none focus:ring-2 focus:ring-ring',
              )}
              title="Reset to localhost default"
            >
              Reset
            </button>
          )}
        </div>

        {/* Reachability indicator. Hidden until the first probe has settled
            so we don't flash "unreachable" during the initial 0-1s of mount. */}
        <div className="flex items-center gap-1.5 text-xs">
          {lastCheckedAt === null ? (
            <span className="text-muted-foreground/50">Checking…</span>
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
