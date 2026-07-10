/**
 * UpdateBanner — a thin top strip that drives the auto-update flow the main
 * process exposes over IPC.
 *
 * The updater runs with autoDownload off, so this is the only surface that
 * turns a detected update into an installed one: it offers "Download", shows
 * progress, then "Restart to update" once the download completes. Without it the
 * app would detect an update (electron/main.js emits `update-available`) and
 * then dead-end. Outside Electron every ipc.onUpdate* call no-ops, so this
 * renders nothing in a browser dev session.
 *
 * Pairs with setupAutoUpdater() in electron/main.js.
 */
import { useEffect, useState } from 'react';
import { ipc } from '@/lib/ipc';

type Phase = 'idle' | 'available' | 'downloading' | 'ready' | 'error';

export function UpdateBanner() {
  const [phase, setPhase] = useState<Phase>('idle');
  const [version, setVersion] = useState<string | null>(null);
  const [percent, setPercent] = useState(0);

  useEffect(() => {
    const offAvailable = ipc.onUpdateAvailable((v) => {
      setVersion(v);
      setPhase('available');
    });
    const offProgress = ipc.onUpdateProgress((p) => {
      setPercent(p);
      setPhase('downloading');
    });
    const offDownloaded = ipc.onUpdateDownloaded(() => setPhase('ready'));
    const offError = ipc.onUpdateError(() => setPhase('error'));
    return () => {
      offAvailable();
      offProgress();
      offDownloaded();
      offError();
    };
  }, []);

  if (phase === 'idle') return null;

  const download = () => {
    setPhase('downloading');
    void ipc.downloadUpdate();
  };
  const restart = () => void ipc.quitAndInstall();

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
        <span>Update failed to download. It will retry on the next launch.</span>
      )}
    </div>
  );
}
