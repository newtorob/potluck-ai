import { create } from 'zustand';
import { localApi } from '@/lib/localApi';
import type { AgentEvent, AgentRun, AgentRunDetail } from '@/types/agents';

interface AgentRunStoreState {
  runs: AgentRun[];
  selectedRunId: string | null;
  detail: AgentRunDetail | null;
  selectedEventId: string | null;
  isFetching: boolean;
  lastError: string | null;
  fetchRuns: () => Promise<void>;
  selectRun: (id: string | null) => Promise<void>;
  pollSelectedRun: () => Promise<void>;
  selectEvent: (event: AgentEvent | null) => void;
}

export const useAgentRunStore = create<AgentRunStoreState>()((set, get) => ({
  runs: [],
  selectedRunId: null,
  detail: null,
  selectedEventId: null,
  isFetching: false,
  lastError: null,

  async fetchRuns() {
    set({ isFetching: true, lastError: null });
    try {
      const runs = await localApi.agents.listRuns();
      set((state) => ({
        runs,
        isFetching: false,
        detail:
          state.detail === null
            ? null
            : {
                ...state.detail,
                run: runs.find((run) => run.id === state.detail?.run.id) ?? state.detail.run,
              },
      }));
    } catch (error) {
      set({
        isFetching: false,
        lastError: error instanceof Error ? error.message : 'Failed to load agent runs',
      });
    }
  },

  async selectRun(id) {
    set({ selectedRunId: id, detail: null, selectedEventId: null, lastError: null });
    if (id === null) return;
    try {
      const detail = await localApi.agents.getRun(id);
      if (get().selectedRunId !== id) return;
      set({
        detail,
        selectedEventId: detail.events.at(-1)?.id ?? null,
      });
    } catch (error) {
      if (get().selectedRunId !== id) return;
      set({
        lastError: error instanceof Error ? error.message : 'Failed to load the run',
      });
    }
  },

  async pollSelectedRun() {
    const { selectedRunId, detail } = get();
    if (selectedRunId === null || detail === null) return;
    try {
      const delta = await localApi.agents.getRunEvents(selectedRunId, detail.next_cursor);
      if (get().selectedRunId !== selectedRunId) return;
      set((state) => {
        if (state.detail === null || state.detail.run.id !== selectedRunId) return state;
        const knownEvents = new Set(state.detail.events.map((event) => event.id));
        const knownResources = new Set(state.detail.resources.map((resource) => resource.id));
        const incoming = delta.events.filter((event) => !knownEvents.has(event.id));
        const previousLatest = state.detail.events.at(-1)?.id ?? null;
        const followsLatest =
          state.selectedEventId === null || state.selectedEventId === previousLatest;
        return {
          detail: {
            ...state.detail,
            instances: delta.instances,
            events: [...state.detail.events, ...incoming],
            resources: [
              ...state.detail.resources,
              ...delta.resources.filter((resource) => !knownResources.has(resource.id)),
            ],
            next_cursor: delta.next_cursor,
          },
          selectedEventId:
            incoming.length > 0 && followsLatest
              ? (incoming.at(-1)?.id ?? state.selectedEventId)
              : state.selectedEventId,
        };
      });
    } catch (error) {
      set({ lastError: error instanceof Error ? error.message : 'Live updates paused' });
    }
  },

  selectEvent(event) {
    set({ selectedEventId: event?.id ?? null });
  },
}));
