/**
 * useProject — thin facade over projectStore.
 *
 * On mount, fetches the project list and seeds the active project from
 * settings (via the settingsStore snapshot) if it hasn't been set yet.
 *
 * Exposes:
 *   projects        — full ProjectSummary list from GET /memory/projects
 *   activeProject   — currently active project name (null = global)
 *   setActive       — PATCH settings + update local state
 *   refreshProjects — re-fetch project list (call after new memory/task created)
 */

import { useEffect } from 'react';
import { useProjectStore } from '@/stores/projectStore';
import { useSettingsStore } from '@/stores/settingsStore';

export function useProject() {
  const { projects, activeProject, isLoading, fetchProjects, setActiveProject, loadActiveFromSettings } =
    useProjectStore();
  const settingsStore = useSettingsStore();

  useEffect(() => {
    // Fetch settings first if not already loaded, then seed active project
    const init = async () => {
      if (!settingsStore.isLoaded) {
        await settingsStore.fetch();
      }
      const settings = useSettingsStore.getState().settings;
      if (settings) {
        loadActiveFromSettings(settings);
      }
      await fetchProjects();
    };
    void init();
    // Run once on mount — all deps are stable Zustand action refs
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    projects,
    activeProject,
    isLoading,
    setActive: setActiveProject,
    refreshProjects: fetchProjects,
  };
}
