/**
 * CoordinatorMismatchBanner - proactive warning when the app's configured
 * coordinator (connectionStore.coordinatorUrl, usually a saved override)
 * disagrees with the coordinator this machine's mesh daemon is actually
 * connected to (connectionStore.sidecarReportedUrl, from the Go mesh
 * daemon's own GET /status via meshStore's polling).
 *
 * This is exactly the failure mode behind a stale dev-testing override: the
 * daemon is correctly on prod, but household/pairing calls go through the
 * app's own (stale) coordinator setting and get rejected by a coordinator
 * that never saw the user's session. Showing this here, before the user
 * even tries "Set up household," turns that into a one-click fix instead of
 * a cryptic auth error - see also `describeCoordinatorAuthError` for the
 * reactive, after-the-failure version of the same fix.
 *
 * Renders nothing when there's no mismatch, or when the mesh bridge/daemon
 * hasn't reported a coordinator yet (sidecarReportedUrl === null) - nothing
 * to compare against in dev or on an unsupported platform.
 *
 * Dismissible per mismatch pair: dismissing this exact (app url, daemon
 * url) combination hides it, but a later, different mismatch (e.g. after
 * switching once, then drifting again some other way) shows again.
 */

import { useState } from 'react';
import { AlertTriangle, X } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { useConnectionStore, isCoordinatorMismatched } from '@/stores/connectionStore';

export function CoordinatorMismatchBanner() {
  const coordinatorUrl = useConnectionStore((s) => s.coordinatorUrl);
  const sidecarReportedUrl = useConnectionStore((s) => s.sidecarReportedUrl);
  const setCoordinatorUrl = useConnectionStore((s) => s.setCoordinatorUrl);

  const [dismissedPair, setDismissedPair] = useState<string | null>(null);

  if (!isCoordinatorMismatched(coordinatorUrl, sidecarReportedUrl) || sidecarReportedUrl === null) {
    return null;
  }

  const pairKey = `${coordinatorUrl}|${sidecarReportedUrl}`;
  if (dismissedPair === pairKey) return null;

  return (
    <div
      role="status"
      className="flex items-start gap-2.5 rounded-xl border border-amber-500/30 bg-amber-500/[0.07] px-3.5 py-3"
    >
      <AlertTriangle
        className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400"
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1 space-y-2">
        <p className="text-sm text-foreground leading-snug">
          This app is set to reach a coordinator at{' '}
          <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">{coordinatorUrl}</code>,
          but this machine's mesh is connected to{' '}
          <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">{sidecarReportedUrl}</code>.
        </p>
        <button
          type="button"
          onClick={() => setCoordinatorUrl(sidecarReportedUrl)}
          className={cn(
            'inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium',
            'bg-primary text-primary-foreground hover:bg-primary/90 transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          Use this machine's coordinator
        </button>
      </div>
      <button
        type="button"
        onClick={() => setDismissedPair(pairKey)}
        aria-label="Dismiss"
        className={cn(
          'shrink-0 rounded-md p-1 text-muted-foreground transition-colors',
          'hover:bg-accent hover:text-foreground',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        )}
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </div>
  );
}
