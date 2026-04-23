/**
 * useMemories — facade over memoryStore.
 *
 * Auto-fetches on mount. Refetches when the window regains focus
 * so stale memory state is refreshed after the user switches away
 * and returns (e.g. after the sidecar processed a conversation).
 *
 * Accepts optional project-scoping params that are forwarded to the store.
 */

import { useEffect, useCallback } from 'react';
import { useMemoryStore } from '@/stores/memoryStore';
import type { MemoryState } from '@/stores/memoryStore';
import type { MemoryCategory } from '@/types/memory';

export type UseMemoriesReturn = MemoryState & {
  refetch: () => void;
};

interface UseMemoriesOptions {
  category?: MemoryCategory;
  limit?: number;
  project?: string | null;
  include_all?: boolean;
}

export function useMemories(opts: UseMemoriesOptions = {}): UseMemoriesReturn {
  const store = useMemoryStore();

  const refetch = useCallback(() => {
    void store.fetchMemories({
      category: opts.category,
      limit: opts.limit,
      project: opts.project,
      include_all: opts.include_all,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, opts.category, opts.limit, opts.project, opts.include_all]);

  // Fetch on mount and whenever filter params change
  useEffect(() => {
    refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opts.category, opts.limit, opts.project, opts.include_all]);

  // Refetch on window focus
  useEffect(() => {
    function handleVisibilityChange() {
      if (document.visibilityState === 'visible') {
        refetch();
      }
    }
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, [refetch]);

  return { ...store, refetch };
}
