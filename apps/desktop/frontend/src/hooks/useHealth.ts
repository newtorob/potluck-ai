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
}

const POLL_INTERVAL_MS = 10_000;

export function useHealth(): UseHealthReturn {
  const [modelLoaded, setModelLoaded] = useState(false);
  const [activeModelId, setActiveModelId] = useState<string | null>(null);
  const [nCtxLoaded, setNCtxLoaded] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      if (document.hidden) return;
      try {
        const health = await localApi.health();
        if (cancelled) return;
        setModelLoaded(health.model_loaded);
        setActiveModelId(health.active_model_id);
        // n_ctx_loaded is optional for backwards compat with older backends.
        // Treat undefined the same as null (no loaded context available).
        setNCtxLoaded(health.n_ctx_loaded ?? null);
      } catch {
        if (cancelled) return;
        setModelLoaded(false);
        setActiveModelId(null);
        setNCtxLoaded(null);
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

  return { modelLoaded, activeModelId, nCtxLoaded };
}
