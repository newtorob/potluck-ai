import { useEffect, useMemo, useState } from 'react';
import { localApi } from '@/lib/localApi';
import {
  parseModelRecommendations,
  type ModelSuggestion,
  type ModelUse,
} from '@/lib/modelRecommendations';

export function useModelRecommendations(use: ModelUse) {
  const [revision, setRevision] = useState(0);
  // A fresh identity also covers general -> coding -> general while requests overlap.
  const request = useMemo(() => ({ use, revision }), [use, revision]);
  const [result, setResult] = useState<{
    request: typeof request;
    models: ModelSuggestion[];
    error: string | null;
  } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void localApi
      .modelSetupOptions(request.use, controller.signal)
      .then((data) => {
        const models = parseModelRecommendations(data, request.use);
        if (!controller.signal.aborted) setResult({ request, models, error: null });
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted)
          setResult({
            request,
            models: [],
            error: err instanceof Error ? err.message : 'Could not read model recommendations.',
          });
      });
    return () => controller.abort();
  }, [request]);

  const current = result?.request === request ? result : null;
  return {
    models: current?.models ?? [],
    error: current?.error ?? null,
    isLoading: current === null,
    refresh: () => setRevision((r) => r + 1),
  };
}
