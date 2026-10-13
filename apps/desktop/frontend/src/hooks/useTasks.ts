/**
 * useTasks - facade over the task store.
 *
 * Auto-fetches on mount and refetches on window focus (for cross-session
 * consistency - an agent may have mutated tasks while the window was blurred).
 */

import { useEffect, useCallback } from 'react';
import { useTaskStore } from '@/stores/taskStore';
import type { TaskStatus } from '@/types/tasks';

interface UseTasksOptions {
  status?: TaskStatus;
  project?: string | null;
  include_all?: boolean;
}

export function useTasks(opts?: UseTasksOptions | TaskStatus) {
  const store = useTaskStore();

  // Accept either a plain TaskStatus string (legacy) or an options object
  const options: UseTasksOptions =
    typeof opts === 'string' ? { status: opts } : (opts ?? {});

  const refetch = useCallback(() => {
    void store.fetchTasks(options);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, options.status, options.project, options.include_all]);

  // Fetch on mount
  useEffect(() => {
    void store.fetchTasks(options);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.status, options.project, options.include_all]);

  // Refetch on window focus - agent may have mutated tasks externally
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
