/**
 * projectStore — Zustand store for project-scoping.
 *
 * Projects are implicit: they exist the moment any memory or task is tagged
 * with a project name. The active_project field in settings is the persistent
 * source of truth; this store mirrors it in-memory for fast reads.
 *
 * State machine:
 *   1. App boots → loadActiveFromSettings() seeds from PotluckSettings
 *   2. fetchProjects() fetches the full list of known tags + counts
 *   3. setActiveProject(name | null) PATCHes settings and updates local state
 */

import { create } from 'zustand';
import { localApi } from '@/lib/localApi';
import type { ProjectSummary } from '@/types/memory';
import type { PotluckSettings } from '@/lib/localApi';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

interface ProjectStoreState {
  projects: ProjectSummary[];
  activeProject: string | null;
  isLoading: boolean;

  // Actions
  fetchProjects: () => Promise<void>;
  setActiveProject: (project: string | null) => Promise<void>;
  loadActiveFromSettings: (settings: PotluckSettings) => void;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useProjectStore = create<ProjectStoreState>()((set) => ({
  projects: [],
  activeProject: null,
  isLoading: false,

  async fetchProjects() {
    set({ isLoading: true });
    try {
      const projects = await localApi.memory.projects();
      set({ projects, isLoading: false });
    } catch {
      // Sidecar may not be running — fail silently, projects are non-critical
      set({ isLoading: false });
    }
  },

  async setActiveProject(project) {
    // Optimistic local update for instant UI response
    set({ activeProject: project });
    try {
      await localApi.updateSettings({ active_project: project });
    } catch {
      // Best-effort; if the PATCH fails we keep the local state anyway
      // since the user's selection intent should persist
    }
  },

  loadActiveFromSettings(settings) {
    set({ activeProject: settings.active_project ?? null });
  },
}));
