/**
 * Task store — single source of truth for agent task state.
 *
 * Optimistic updates for status changes with rollback on failure.
 * The store does not auto-fetch — that is the responsibility of useTasks.
 *
 * See guide §9.5.
 */

import { create } from 'zustand';
import { localApi } from '@/lib/localApi';
import type { TaskState, TaskStatus, TaskCreateInput, TaskUpdateInput } from '@/types/tasks';

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------

interface TaskStoreState {
  tasks: TaskState[];
  isFetching: boolean;
  fetchError: string | null;

  // Actions
  fetchTasks: (opts?: { status?: TaskStatus; project?: string | null; include_all?: boolean }) => Promise<void>;
  createTask: (input: TaskCreateInput) => Promise<TaskState>;
  updateTask: (id: string, patch: TaskUpdateInput) => Promise<void>;
  completeStep: (id: string, step: string) => Promise<void>;
  addError: (id: string, error: string, resolution?: string) => Promise<void>;
  deleteTask: (id: string) => Promise<void>;
  setStatus: (id: string, status: TaskStatus) => Promise<void>;
}

// ---------------------------------------------------------------------------
// Store implementation
// ---------------------------------------------------------------------------

export const useTaskStore = create<TaskStoreState>()((set, get) => ({
  tasks: [],
  isFetching: false,
  fetchError: null,

  // ---- fetchTasks ----------------------------------------------------------

  async fetchTasks(opts) {
    set({ isFetching: true, fetchError: null });
    try {
      const tasks = await localApi.tasks.list({
        ...(opts?.status !== undefined ? { status: opts.status } : {}),
        ...(opts?.project !== undefined ? { project: opts.project } : {}),
        ...(opts?.include_all !== undefined ? { include_all: opts.include_all } : {}),
      });
      set({ tasks, isFetching: false });
    } catch (err) {
      set({
        isFetching: false,
        fetchError: err instanceof Error ? err.message : 'Failed to load tasks',
      });
    }
  },

  // ---- createTask ----------------------------------------------------------

  async createTask(input) {
    const task = await localApi.tasks.create(input);
    set((s) => ({ tasks: [task, ...s.tasks] }));
    return task;
  },

  // ---- updateTask ----------------------------------------------------------

  async updateTask(id, patch) {
    const previous = get().tasks;
    // Optimistic: apply patch fields locally
    set((s) => ({
      tasks: s.tasks.map((t) =>
        t.id === id ? { ...t, ...patch, updated_at: new Date().toISOString() } : t,
      ),
    }));
    try {
      const updated = await localApi.tasks.update(id, patch);
      set((s) => ({ tasks: s.tasks.map((t) => (t.id === id ? updated : t)) }));
    } catch (err) {
      // Rollback
      set({ tasks: previous });
      throw err;
    }
  },

  // ---- completeStep --------------------------------------------------------

  async completeStep(id, step) {
    const previous = get().tasks;
    // Optimistic: move step from pending to completed
    set((s) => ({
      tasks: s.tasks.map((t) => {
        if (t.id !== id) return t;
        return {
          ...t,
          pending_steps: t.pending_steps.filter((s) => s !== step),
          completed_steps: [...t.completed_steps, step],
          updated_at: new Date().toISOString(),
        };
      }),
    }));
    try {
      const updated = await localApi.tasks.completeStep(id, step);
      set((s) => ({ tasks: s.tasks.map((t) => (t.id === id ? updated : t)) }));
    } catch (err) {
      set({ tasks: previous });
      throw err;
    }
  },

  // ---- addError ------------------------------------------------------------

  async addError(id, error, resolution) {
    const body = resolution !== undefined ? { error, resolution } : { error };
    const updated = await localApi.tasks.addError(id, body);
    set((s) => ({ tasks: s.tasks.map((t) => (t.id === id ? updated : t)) }));
  },

  // ---- deleteTask ----------------------------------------------------------

  async deleteTask(id) {
    await localApi.tasks.delete(id);
    set((s) => ({ tasks: s.tasks.filter((t) => t.id !== id) }));
  },

  // ---- setStatus -----------------------------------------------------------
  // Convenience wrapper for action buttons (start / pause / complete / abandon).

  async setStatus(id, status) {
    await get().updateTask(id, { status });
  },
}));
