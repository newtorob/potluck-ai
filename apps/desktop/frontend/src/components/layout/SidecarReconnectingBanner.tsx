/**
 * SidecarReconnectingBanner - a thin top strip shown only when the local Python
 * engine drops after it has been healthy at least once this session.
 *
 * Why the latch: useHealth starts sidecarUp=false and only flips true on the
 * first successful /health poll, so a banner keyed on !sidecarUp alone would
 * flash on every launch before the first poll resolves. We only surface it once
 * we have actually seen the engine up, so it means "it dropped", not "still
 * booting".
 *
 * Pairs with the main-process crash circuit-breaker (electron/main.js): while
 * the sidecar auto-restarts, or after the user hits "Try again" on the crash
 * dialog, this tells the user the app knows and is recovering, instead of
 * leaving the UI throwing silent API errors. The /health poll clears it the
 * moment the engine responds again.
 */
import { useEffect, useState } from 'react';
import { useHealth } from '@/hooks/useHealth';

export function SidecarReconnectingBanner() {
  const { sidecarUp } = useHealth();
  const [wasEverUp, setWasEverUp] = useState(false);

  useEffect(() => {
    if (sidecarUp) setWasEverUp(true);
  }, [sidecarUp]);

  if (!wasEverUp || sidecarUp) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex h-7 shrink-0 items-center justify-center gap-2 border-b border-amber-500/30 bg-amber-500/10 px-4 text-[12px] text-amber-700 dark:text-amber-300"
    >
      <span
        className="inline-block h-1.5 w-1.5 rounded-full bg-amber-500 animate-hearth-pulse"
        aria-hidden="true"
      />
      Reconnecting to the local engine. Your saved chats are safe.
    </div>
  );
}
