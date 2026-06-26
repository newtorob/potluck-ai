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
import { localApi, type PipelineStatus } from '@/lib/localApi';

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
  /** Pipeline-parallel: the distributed engine's status, or null when the
   *  current model isn't loaded with the pipeline strategy. */
  loadedPipeline: PipelineStatus | null;
  /** Shared (continuous) batching: true when the loaded model is served through
   *  a continuous batcher (whole-model / pipeline). False on the single-stream
   *  and llama.cpp paths, and when nothing is loaded. */
  continuousBatch: boolean;
}

const POLL_INTERVAL_MS = 10_000;

const INITIAL_STATE: UseHealthReturn = {
  modelLoaded: false,
  activeModelId: null,
  nCtxLoaded: null,
  sidecarUp: false,
  loadedRpcServers: null,
  loadedPipeline: null,
  continuousBatch: false,
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
          // loaded_pipeline can carry either the pipeline-parallel engine or the
          // whole-model engine (same slot, discriminated by `strategy`). The
          // PipelineModeCard only wants the pipeline shape, so keep it null for
          // whole-model. continuousBatch is the cross-cutting flag both paths share.
          loadedPipeline:
            health.loaded_pipeline?.strategy === 'pipeline'
              ? health.loaded_pipeline
              : null,
          continuousBatch: health.continuous_batch ?? false,
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
