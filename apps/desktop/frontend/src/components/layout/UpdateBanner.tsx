/**
 * UpdateBanner — a thin top strip that drives the auto-update flow exposed
 * over window.potluck.updater.
 *
 * On mount, if the build is packaged and the updater bridge is present, it
 * kicks off a check(). It then subscribes to the updater's events for the
 * lifetime of the app shell (this component is never unmounted while the
 * user is signed in) and mirrors them into updateStore — the same store the
 * version-handshake "soft available" nudge (see updateStore.checkVersionHandshake,
 * driven from App.tsx) and the Settings status line read from, so all three
 * surfaces agree.
 *
 * Outside Electron, or before main/preload ship window.potluck.updater,
 * every ipc.onUpdate* call is a safe no-op — this renders nothing there.
 *
 * Pairs with the updater wiring in electron/main.js.
 */
import { useEffect } from 'react';
import { X } from 'lucide-react';
import { ipc } from '@/lib/ipc';
import { useUpdateStore } from '@/stores/updateStore';

export function UpdateBanner() {
  const phase = useUpdateStore((s) => s.phase);
  const version = useUpdateStore((s) => s.version);
  const percent = useUpdateStore((s) => s.percent);
  const dismissed = useUpdateStore((s) => s.dismissed);
  const errorMessage = useUpdateStore((s) => s.errorMessage);

  useEffect(() => {
    const offAvailable = ipc.onUpdateAvailable((v) => useUpdateStore.getState().setAvailable(v));
    const offNotAvailable = ipc.onUpdateNotAvailable(() =>
      useUpdateStore.getState().setNotAvailable(),
    );
    const offProgress = ipc.onUpdateProgress((p) => useUpdateStore.getState().setProgress(p));
    const offDownloaded = ipc.onUpdateDownloaded(() => useUpdateStore.getState().setDownloaded());
    const offError = ipc.onUpdateError((m) => useUpdateStore.getState().setError(m));

    // Kick off a check on mount. Only meaningful in a packaged build with
    // the updater bridge present — buildInfo defaults to "packaged" when
    // absent (an older build predating buildInfo), since ipc.hasUpdater is
    // the real safety gate: check() is a no-op whenever the bridge is gone.
    const packaged = ipc.buildInfo?.packaged !== false;
    if (packaged && ipc.hasUpdater) {
      useUpdateStore.getState().check();
    }

    return () => {
      offAvailable();
      offNotAvailable();
      offProgress();
      offDownloaded();
      offError();
    };
  }, []);

  if (phase === 'idle') return null;
  if (phase === 'available' && dismissed) return null;

  const download = () => useUpdateStore.getState().download();
  const restart = () => useUpdateStore.getState().install();
  const dismiss = () => useUpdateStore.getState().dismiss();
  const retry = () => useUpdateStore.getState().download();

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex h-7 shrink-0 items-center justify-center gap-3 border-b border-sky-500/30 bg-sky-500/10 px-4 text-[12px] text-sky-700 dark:text-sky-300"
    >
      {phase === 'available' && (
        <>
          <span>A new version{version ? ` (${version})` : ''} is available.</span>
          <button
            type="button"
            onClick={download}
            className="font-medium underline underline-offset-2 hover:no-underline"
          >
            Download
          </button>
          <button
            type="button"
            onClick={dismiss}
            aria-label="Dismiss"
            className="text-sky-700/70 hover:text-sky-700 dark:text-sky-300/70 dark:hover:text-sky-300"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </>
      )}

      {phase === 'downloading' && <span>Downloading update… {percent}%</span>}

      {phase === 'ready' && (
        <>
          <span>Update ready.</span>
          <button
            type="button"
            onClick={restart}
            className="font-medium underline underline-offset-2 hover:no-underline"
          >
            Restart to update
          </button>
        </>
      )}

      {phase === 'error' && (
        <>
          <span>Update failed to download{errorMessage ? `: ${errorMessage}` : ''}.</span>
          <button
            type="button"
            onClick={retry}
            className="font-medium underline underline-offset-2 hover:no-underline"
          >
            Retry
          </button>
        </>
      )}
    </div>
  );
}
