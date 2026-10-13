/**
 * useMemories - facade over memoryStore.
 *
 * Auto-fetches on mount. Refetches when the window regains focus
 * so stale memory state is refreshed after the user switches away
 * and returns (e.g. after the sidecar processed a conversation).
 *
 * Accepts optional project-scoping params that are forwarded to the store.
 *
 * performSearch is debounced 300ms here at the hook layer so individual
 * components don't need to manage their own debounce timers.
 */

import { useEffect, useCallback, useRef } from 'react';
import { useMemoryStore } from '@/stores/memoryStore';
import type { MemoryState } from '@/stores/memoryStore';
import type { MemoryCategory } from '@/types/memory';

export type UseMemoriesReturn = Omit<MemoryState, 'performSearch'> & {
  refetch: () => void;
  performSearch: (
    query: string,
    opts?: { project?: string | null; min_confidence?: number },
  ) => void;
};

interface UseMemoriesOptions {
  category?: MemoryCategory;
  limit?: number;
  project?: string | null;
  include_all?: boolean;
}

export function useMemories(opts: UseMemoriesOptions = {}): UseMemoriesReturn {
  const store = useMemoryStore();
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refetch = useCallback(() => {
    void store.fetchMemories({
      ...(opts.category !== undefined ? { category: opts.category } : {}),
      ...(opts.limit !== undefined ? { limit: opts.limit } : {}),
      ...(opts.project !== undefined ? { project: opts.project } : {}),
      ...(opts.include_all !== undefined ? { include_all: opts.include_all } : {}),
    });
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

  // Debounced search - 300ms; cancels in-flight debounce on each new call
  const performSearch = useCallback(
    (query: string, searchOpts?: { project?: string | null; min_confidence?: number }) => {
      if (debounceRef.current !== null) {
        clearTimeout(debounceRef.current);
      }
      if (!query.trim()) {
        store.clearSearch();
        return;
      }
      debounceRef.current = setTimeout(() => {
        void store.performSearch(query, searchOpts);
      }, 300);
    },
    // store is a stable Zustand ref; clearSearch and performSearch are stable action refs

    [store],
  );

  // Clean up pending debounce on unmount
  useEffect(() => {
    return () => {
      if (debounceRef.current !== null) {
        clearTimeout(debounceRef.current);
      }
    };
  }, []);

  return { ...store, refetch, performSearch };
}
