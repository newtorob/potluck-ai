/**
 * useCoordinatorHealth - lightweight reachability probe for the Phase G
 * coordinator. Polls `localApi.network.health()` on an interval and
 * returns whether the coordinator answered a 2xx within the timeout.
 *
 * Used to gate UI affordances that only make sense when the coordinator
 * is up - primarily the chat composer's "Network mode" toggle. The
 * coordinator running in Docker (`make up`) is the dev assumption; in
 * production the coordinator is hosted by the cooperative and should be
 * reachable nearly always, but transient outages or the user being
 * offline should degrade gracefully.
 *
 * Polling intervals:
 *   - `intervalMs` (default 10s) while the coordinator is reachable
 *   - On unreachable transition, immediately retry at 2s for the first 3
 *     attempts so the UI bounces back quickly after `make up`
 *   - Pauses entirely when document.hidden is true (battery + req budget)
 */
import { useEffect, useRef, useState } from 'react';
import { localApi } from '@/lib/localApi';
import { useConnectionStore } from '@/stores/connectionStore';

export interface CoordinatorHealth {
  /** True if the most recent probe got a 2xx within the timeout window. */
  reachable: boolean;
  /** Epoch ms of the most recent successful or failed check. */
  lastCheckedAt: number | null;
}

const DEFAULT_INTERVAL_MS = 10_000;
const FAST_RETRY_INTERVAL_MS = 2_000;
const FAST_RETRY_BUDGET = 3;

export function useCoordinatorHealth(intervalMs: number = DEFAULT_INTERVAL_MS): CoordinatorHealth {
  const [state, setState] = useState<CoordinatorHealth>({
    reachable: false,
    lastCheckedAt: null,
  });

  // Subscribe to the coordinator URL so that when it changes the effect
  // re-runs immediately, cancelling the old timer chain and probing the
  // new URL without waiting up to 10s for the next scheduled tick.
  const coordinatorUrl = useConnectionStore((s) => s.coordinatorUrl);

  // Track the live reachability value in a ref so the check() closure can
  // read it synchronously without capturing stale state from the initial
  // render. This is the canonical pattern when effect logic needs to act on
  // the current value before enqueuing a setState call.
  const reachableRef = useRef<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;
    let fastRetriesRemaining = 0;

    const scheduleNext = (delayMs: number) => {
      if (cancelled) return;
      timeoutId = setTimeout(() => void check(), delayMs);
    };

    const check = async () => {
      if (cancelled) return;
      // Pause when tab is hidden - no point burning probes while invisible.
      if (typeof document !== 'undefined' && document.hidden) {
        scheduleNext(intervalMs);
        return;
      }
      const ok = await localApi.network.health();
      if (cancelled) return;
      // On reachable → unreachable transition, kick off the fast-retry budget
      // so we notice when the coordinator comes back without waiting a full
      // interval. Reading from the ref gives us the live current value - the
      // stale closure over `state` would always see the initial false, and
      // the updater-callback form of setState runs asynchronously after this
      // synchronous code, so by the time the updater ran, this check would
      // already have completed with fastRetriesRemaining still at 0.
      if (reachableRef.current && !ok) {
        fastRetriesRemaining = FAST_RETRY_BUDGET;
      }
      reachableRef.current = ok;
      setState({ reachable: ok, lastCheckedAt: Date.now() });
      if (!ok && fastRetriesRemaining > 0) {
        fastRetriesRemaining -= 1;
        scheduleNext(FAST_RETRY_INTERVAL_MS);
      } else {
        scheduleNext(intervalMs);
      }
    };

    // Kick off the first probe immediately.
    void check();

    const onVisibility = () => {
      // Coming back from hidden → re-probe right away so the UI updates
      // without waiting for the next interval tick.
      if (!document.hidden) {
        if (timeoutId !== null) clearTimeout(timeoutId);
        void check();
      }
    };
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility);
    }

    return () => {
      cancelled = true;
      if (timeoutId !== null) clearTimeout(timeoutId);
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility);
      }
    };
  // coordinatorUrl is included so the effect re-runs (cancels old timers,
  // re-probes immediately) whenever the user edits the coordinator URL in
  // CooperativeConnectionPanel. Without it the old URL's result stays on
  // screen for up to intervalMs.
  }, [intervalMs, coordinatorUrl]);

  return state;
}
