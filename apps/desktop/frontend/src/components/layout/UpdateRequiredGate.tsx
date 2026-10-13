/**
 * UpdateRequiredGate - blocking full-screen "update required" screen.
 *
 * Rendered from App.tsx, above AuthGate/the whole app shell, whenever
 * updateStore.blocked is true (the running version is below `minimum` from
 * the GET /client/version handshake - see App.tsx's startup effect and
 * updateStore.checkVersionHandshake). This is the universal backstop:
 * whatever changed on the backend, an old client shows this one clear
 * screen instead of cryptic downstream failures.
 *
 * Shares updateStore's phase machine with UpdateBanner, so a download
 * already in flight (started from the banner before the gate could even
 * mount, or from a prior session) carries straight through here. On mount
 * it also kicks off its own check() - the gate can appear before
 * UpdateBanner ever gets a chance to (it replaces the whole app shell,
 * UpdateBanner included), so it can't rely on the banner having started one.
 */
import { useEffect } from 'react';
import { Download, RefreshCw } from 'lucide-react';
import { ipc } from '@/lib/ipc';
import { useUpdateStore } from '@/stores/updateStore';
import { SafeExternalLink } from '@/components/ui/SafeExternalLink';

export function UpdateRequiredGate() {
  const phase = useUpdateStore((s) => s.phase);
  const percent = useUpdateStore((s) => s.percent);
  const latestVersion = useUpdateStore((s) => s.latestVersion);
  const notesUrl = useUpdateStore((s) => s.notesUrl);
  const errorMessage = useUpdateStore((s) => s.errorMessage);

  useEffect(() => {
    if (ipc.hasUpdater) useUpdateStore.getState().check();
  }, []);

  const hasUpdater = ipc.hasUpdater;
  const fallbackHref = notesUrl ?? 'https://trypotluck.ai';

  const primaryAction = () => {
    if (phase === 'ready') useUpdateStore.getState().install();
    else useUpdateStore.getState().download();
  };

  const primaryLabel =
    phase === 'downloading'
      ? `Downloading… ${percent}%`
      : phase === 'ready'
        ? 'Restart to update'
        : phase === 'error'
          ? 'Try again'
          : 'Update now';

  return (
    <div
      role="alert"
      aria-live="assertive"
      className="flex h-screen w-screen flex-col items-center justify-center gap-6 bg-background p-8 text-center"
    >
      <div className="max-w-sm space-y-3">
        <p className="font-serif text-xl font-light text-foreground">Update required</p>
        <p className="font-sans text-sm leading-relaxed text-muted-foreground">
          This version of Potluck is too old to keep working with the network
          {latestVersion ? ` (the current version is ${latestVersion})` : ''}. Update to
          keep going.
        </p>
        {phase === 'error' && errorMessage && (
          <p className="font-sans text-xs text-destructive">{errorMessage}</p>
        )}
      </div>

      {hasUpdater && (
        <button
          type="button"
          onClick={primaryAction}
          disabled={phase === 'downloading'}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-5 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {phase === 'ready' ? (
            <RefreshCw className="h-4 w-4" />
          ) : (
            <Download className="h-4 w-4" />
          )}
          {primaryLabel}
        </button>
      )}

      <p className="max-w-sm font-sans text-xs text-muted-foreground">
        {hasUpdater
          ? "If auto-update doesn't work, download the latest build directly: "
          : 'Auto-update is not available here. Download the latest build directly: '}
        <SafeExternalLink
          href={fallbackHref}
          className="underline underline-offset-2 hover:no-underline"
        >
          {fallbackHref.replace(/^https?:\/\//, '')}
        </SafeExternalLink>
      </p>
    </div>
  );
}
