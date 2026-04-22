/**
 * useMemories — facade over memoryStore.
 *
 * Auto-fetches on mount. Refetches when the window regains focus
 * so stale memory state is refreshed after the user switches away
 * and returns (e.g. after the sidecar processed a conversation).
 */

import { useEffect } from 'react';
import { useMemoryStore } from '@/stores/memoryStore';
import type { MemoryState } from '@/stores/memoryStore';

export type UseMemoriesReturn = MemoryState;

export function useMemories(): UseMemoriesReturn {
  const store = useMemoryStore();

  // Fetch on mount
  useEffect(() => {
    void store.fetchMemories();
    // fetchMemories is a stable Zustand action ref
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Refetch on window focus
  useEffect(() => {
    function handleVisibilityChange() {
      if (document.visibilityState === 'visible') {
        void store.fetchMemories();
      }
    }
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return store;
}
