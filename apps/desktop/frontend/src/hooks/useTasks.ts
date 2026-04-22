/**
 * useTasks — facade over the task store.
 *
 * Auto-fetches on mount and refetches on window focus (for cross-session
 * consistency — an agent may have mutated tasks while the window was blurred).
 */

import { useEffect, useCallback } from 'react';
import { useTaskStore } from '@/stores/taskStore';
import type { TaskStatus } from '@/types/tasks';

export function useTasks(statusFilter?: TaskStatus) {
  const store = useTaskStore();

  const refetch = useCallback(() => {
    void store.fetchTasks(statusFilter);
  }, [store, statusFilter]);

  // Fetch on mount
  useEffect(() => {
    void store.fetchTasks(statusFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  // Refetch on window focus — agent may have mutated tasks externally
  useEffect(() => {
    window.addEventListener('focus', refetch);
    return () => window.removeEventListener('focus', refetch);
  }, [refetch]);

  return {
    tasks: store.tasks,
    isFetching: store.isFetching,
    fetchError: store.fetchError,
    createTask: store.createTask,
    updateTask: store.updateTask,
    completeStep: store.completeStep,
    addError: store.addError,
    deleteTask: store.deleteTask,
    setStatus: store.setStatus,
    refetch,
  };
}
