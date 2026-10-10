/**
 * acceptanceStore: the engine's answer check for the loaded model, read from
 * GET /health/inference.
 *
 * Read when the app connects to the engine and after a model load completes
 * (the AnswerCheckBanner and the model picker call `refresh`). The check can
 * still be running when a load returns, so a `running` answer is asked again
 * every few seconds, for a couple of minutes at most.
 *
 * Tolerant by construction: an engine without the route or the key leaves
 * `acceptance` null, and a failed read keeps what was known before.
 *
 * `dismissedKey` remembers which state the person closed the banner for. It
 * lives only as long as the app is open and stops matching as soon as the
 * model or the verdict changes.
 */

import { create } from 'zustand';
import { localApi } from '@/lib/localApi';
import { acceptanceFromHealth, bannerKey, type Acceptance } from '@/lib/acceptance';

const RUNNING_RECHECK_MS = 3_000;
const RUNNING_RECHECK_LIMIT = 40;

interface AcceptanceState {
  /** Null when nothing is known: older engine, no model, or not read yet. */
  acceptance: Acceptance | null;
  /** The banner state the person dismissed for this session. */
  dismissedKey: string | null;
  /** Read the check now. Safe to call often: a read already in flight is reused. */
  refresh: () => Promise<void>;
  /** Close the banner for the state it is showing. */
  dismiss: () => void;
}

let inFlight: Promise<void> | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let rechecks = 0;

export const useAcceptanceStore = create<AcceptanceState>()((set, get) => {
  function read(): Promise<void> {
    if (inFlight !== null) return inFlight;
    inFlight = (async () => {
      try {
        const next = acceptanceFromHealth(await localApi.inferenceHealth());
        set({ acceptance: next });
        if (next?.status === 'running' && rechecks < RUNNING_RECHECK_LIMIT) {
          rechecks += 1;
          timer = setTimeout(() => {
            timer = null;
            void read();
          }, RUNNING_RECHECK_MS);
        }
      } catch {
        // The route is missing, the engine is busy or not up yet: keep what was known.
      } finally {
        inFlight = null;
      }
    })();
    return inFlight;
  }

  return {
    acceptance: null,
    dismissedKey: null,

    refresh() {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      rechecks = 0;
      return read();
    },

    dismiss() {
      const current = get().acceptance;
      if (current !== null) set({ dismissedKey: bannerKey(current) });
    },
  };
});
