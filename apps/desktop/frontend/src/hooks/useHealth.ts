/**
 * useHealth — polls GET /health on a 10-second interval.
 *
 * Exposes the three values ChatView's top-bar chips need:
 *   modelLoaded   — whether any model is currently loaded into the runtime
 *   activeModelId — slug of the loaded model, or null
 *   nCtxLoaded    — context window the runtime was actually initialised with;
 *                   null when no model is loaded
 *
 * Polling pauses while the document is hidden (tab backgrounded) and
 * resumes the moment it becomes visible again, matching the pattern used
 * by useModels.
 */

import { useEffect, useState } from 'react';
import { localApi } from '@/lib/localApi';

export interface UseHealthReturn {
  modelLoaded: boolean;
  activeModelId: string | null;
  nCtxLoaded: number | null;
  /** True when the most recent /health call returned 2xx. Decoupled
   *  from model state: the sidecar can be up with no model loaded. */
  sidecarUp: boolean;
  /** Big Model Mode: list of RPC endpoints the current model is
   *  distributed across. Null when loaded locally or no model loaded. */
  loadedRpcServers: string[] | null;
}

const POLL_INTERVAL_MS = 10_000;

const INITIAL_STATE: UseHealthReturn = {
  modelLoaded: false,
  activeModelId: null,
  nCtxLoaded: null,
  sidecarUp: false,
  loadedRpcServers: null,
};

export function useHealth(): UseHealthReturn {
  const [state, setState] = useState<UseHealthReturn>(INITIAL_STATE);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      if (document.hidden) return;
      try {
        const health = await localApi.health();
        if (cancelled) return;
        setState({
          modelLoaded: health.model_loaded,
          activeModelId: health.active_model_id,
          // n_ctx_loaded is optional for backwards compat with older backends.
          // Treat undefined the same as null (no loaded context available).
          nCtxLoaded: health.n_ctx_loaded ?? null,
          sidecarUp: true,
          loadedRpcServers: health.loaded_rpc_servers ?? null,
        });
      } catch {
        if (cancelled) return;
        setState(INITIAL_STATE);
      }
    }

    void poll();

    const interval = setInterval(() => void poll(), POLL_INTERVAL_MS);

    function handleVisibility() {
      if (document.visibilityState === 'visible') {
        void poll();
      }
    }

    document.addEventListener('visibilitychange', handleVisibility);

    return () => {
      cancelled = true;
      clearInterval(interval);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, []);

  return state;
}
