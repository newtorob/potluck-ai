/**
 * MeshServiceOutdatedBanner: shown on My Machines while the mesh service that
 * answers is the sidecar from before the last app update.
 *
 * The mesh service runs as a system service that outlives the app, so an app
 * update replaces the binary on disk but leaves the old process running. A
 * current sidecar restarts itself when its binary changes; an older one never
 * does. main.js (see "Mesh service version check") notices the difference and
 * reports daemon state 'outdated' while the mesh itself keeps working, which is
 * why this is a banner above the normal view and not a full-page panel like
 * MeshEnablePanel's states.
 *
 * The button runs meshDaemon.enable(), the existing install path, which
 * restarts the service: on Linux and Windows that shows the system's
 * authentication prompt, on macOS it refreshes the daemon's registration and
 * then either asks the running service to restart or, for a service old enough
 * to have no such command, asks for administrator rights to stop it. Nothing
 * prompts until the user presses it. On macOS a restart of the whole Mac does
 * the same, so the line says so.
 *
 * Renders nothing when the store has no 'outdated' daemon status.
 */

import { useState } from 'react';
import { AlertTriangle, Loader2, RotateCw } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { meshDaemon } from '@/lib/meshClient';
import { useMeshStore } from '@/stores/meshStore';

const RESTART_LINE = 'The mesh service is still running the previous version. Restart it to finish the update.';
const RESTART_LINE_MAC =
  'The mesh service is still running the previous version. Restart it to finish the update, or restart your Mac.';

export function MeshServiceOutdatedBanner() {
  const serviceOutdated = useMeshStore((s) => s.serviceOutdated);
  const refresh = useMeshStore((s) => s.refresh);

  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (serviceOutdated === null) return null;

  async function handleRestart() {
    setError(null);
    setWorking(true);
    try {
      const result = await meshDaemon.enable();
      if (result.state === 'not_installed' || result.state === 'failed') {
        setError(result.detail ?? 'The mesh service could not be restarted. Please try again.');
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The mesh service could not be restarted.');
    } finally {
      setWorking(false);
    }
  }

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
          {serviceOutdated.platform === 'darwin' ? RESTART_LINE_MAC : RESTART_LINE}
        </p>
        <button
          type="button"
          onClick={() => void handleRestart()}
          disabled={working}
          className={cn(
            'inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium',
            'bg-primary text-primary-foreground hover:bg-primary/90 transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        >
          {working ? (
            <>
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
              Restarting…
            </>
          ) : (
            <>
              <RotateCw className="h-3 w-3" aria-hidden="true" />
              Restart mesh service
            </>
          )}
        </button>
        {error !== null && (
          <p className="text-xs text-destructive" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
