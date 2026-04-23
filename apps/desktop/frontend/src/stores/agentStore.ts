/**
 * Agent observability store.
 *
 * Holds sessions, activity feed, and selected-session detail.
 * Polling control lives in useAgents — the store is pure state + actions.
 *
 * Design notes:
 * - `activity` is prepend-merged on each fetch so new calls slide in at
 *   the top without re-rendering the entire list (uses id dedup).
 * - `selectedSessionId` drives the SessionDetailDrawer; fetchSessionDetail
 *   is called explicitly when a card is clicked, not automatically.
 */

import { create } from 'zustand';
import { localApi } from '@/lib/localApi';
import type { AgentSession, SessionDetail, ToolCall } from '@/types/agents';

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------

interface ActivityFilter {
  client?: string;
  project?: string;
  tool?: string;
}

interface AgentStoreState {
  sessions: AgentSession[];
  activity: ToolCall[];
  isFetching: boolean;
  lastError: string | null;
  selectedSessionId: string | null;
  sessionDetail: SessionDetail | null;
  filter: ActivityFilter;

  // Actions
  fetchSessions: (opts?: { include_disconnected?: boolean; limit?: number }) => Promise<void>;
  fetchActivity: (opts?: { limit?: number }) => Promise<void>;
  fetchSessionDetail: (id: string) => Promise<void>;
  setFilter: (patch: Partial<ActivityFilter>) => void;
  clearFilter: () => void;
  selectSession: (id: string | null) => void;
}

// ---------------------------------------------------------------------------
// Store implementation
// ---------------------------------------------------------------------------

export const useAgentStore = create<AgentStoreState>()((set, get) => ({
  sessions: [],
  activity: [],
  isFetching: false,
  lastError: null,
  selectedSessionId: null,
  sessionDetail: null,
  filter: {},

  // ---- fetchSessions -------------------------------------------------------

  async fetchSessions(opts) {
    set({ isFetching: true, lastError: null });
    try {
      const sessions = await localApi.agents.listSessions(opts ?? {});
      set({ sessions, isFetching: false });
    } catch (err) {
      set({
        isFetching: false,
        lastError: err instanceof Error ? err.message : 'Failed to load sessions',
      });
    }
  },

  // ---- fetchActivity -------------------------------------------------------
  // Prepend-merges new calls to the top so the feed animates in naturally.
  // The feed is capped at 200 entries to prevent unbounded growth.

  async fetchActivity(opts) {
    const { filter } = get();
    try {
      const incoming = await localApi.agents.activity({
        limit: opts?.limit ?? 100,
        ...(filter.client ? { client: filter.client } : {}),
        ...(filter.project ? { project: filter.project } : {}),
        ...(filter.tool ? { tool: filter.tool } : {}),
      });

      set((s) => {
        const existingIds = new Set(s.activity.map((c) => c.id));
        const fresh = incoming.filter((c) => !existingIds.has(c.id));
        // Prepend fresh calls, keep existing, cap total at 200
        const merged = [...fresh, ...s.activity].slice(0, 200);
        return { activity: merged };
      });
    } catch (err) {
      set({
        lastError: err instanceof Error ? err.message : 'Failed to load activity',
      });
    }
  },

  // ---- fetchSessionDetail --------------------------------------------------

  async fetchSessionDetail(id) {
    try {
      const detail = await localApi.agents.getSession(id);
      set({ sessionDetail: detail });
    } catch (err) {
      set({
        lastError: err instanceof Error ? err.message : 'Failed to load session detail',
      });
    }
  },

  // ---- filter helpers ------------------------------------------------------

  setFilter(patch) {
    set((s) => ({ filter: { ...s.filter, ...patch }, activity: [] }));
    // Caller (useAgents) will re-fetch activity after filter change
  },

  clearFilter() {
    set({ filter: {}, activity: [] });
  },

  // ---- selectSession -------------------------------------------------------

  selectSession(id) {
    set({ selectedSessionId: id, sessionDetail: id === null ? null : get().sessionDetail });
    if (id !== null) {
      void get().fetchSessionDetail(id);
    }
  },
}));
