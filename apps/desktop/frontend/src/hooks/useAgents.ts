/**
 * useAgents — facade over the agent store with polling lifecycle.
 *
 * Polling strategy:
 *   - Sessions: every 5 seconds (live "active now" feel)
 *   - Activity: every 10 seconds (lower frequency to reduce traffic)
 *   - Both intervals pause when document.hidden (window blurred / tab not visible)
 *   - Intervals resume immediately on visibilitychange to visible
 *   - Filter changes reset activity and re-fetch immediately (via effect dep)
 *
 * The store handles prepend-merge so the activity feed doesn't flash on poll.
 */

import { useEffect, useRef, useCallback } from 'react';
import { useAgentStore } from '@/stores/agentStore';

const SESSIONS_INTERVAL_MS = 5_000;
const ACTIVITY_INTERVAL_MS = 10_000;

export function useAgents() {
  const store = useAgentStore();
  const { filter } = store;

  // Stable refs to the latest store actions — avoids stale closure in interval callbacks
  const fetchSessionsRef = useRef(store.fetchSessions);
  const fetchActivityRef = useRef(store.fetchActivity);
  fetchSessionsRef.current = store.fetchSessions;
  fetchActivityRef.current = store.fetchActivity;

  // ---- Initial fetch -------------------------------------------------------

  useEffect(() => {
    void fetchSessionsRef.current();
    void fetchActivityRef.current();
  }, []);

  // Re-fetch activity when filter changes (sessions fetch is independent)
  const filterKey = JSON.stringify(filter);
  useEffect(() => {
    void fetchActivityRef.current();
    // filterKey is a stable JSON string — safe dep
  }, [filterKey]);

  // ---- Polling loops -------------------------------------------------------

  const sessionsTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activityTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimers = useCallback(() => {
    if (sessionsTimerRef.current !== null) {
      clearTimeout(sessionsTimerRef.current);
      sessionsTimerRef.current = null;
    }
    if (activityTimerRef.current !== null) {
      clearTimeout(activityTimerRef.current);
      activityTimerRef.current = null;
    }
  }, []);

  const scheduleSessions = useCallback(() => {
    sessionsTimerRef.current = setTimeout(() => {
      if (!document.hidden) {
        void fetchSessionsRef.current();
      }
      scheduleSessions();
    }, SESSIONS_INTERVAL_MS);
  }, []);

  const scheduleActivity = useCallback(() => {
    activityTimerRef.current = setTimeout(() => {
      if (!document.hidden) {
        void fetchActivityRef.current();
      }
      scheduleActivity();
    }, ACTIVITY_INTERVAL_MS);
  }, []);

  useEffect(() => {
    scheduleSessions();
    scheduleActivity();

    // Pause on hidden, resume on visible — refetch immediately on resume
    function handleVisibilityChange() {
      if (!document.hidden) {
        // Refetch right away when coming back into focus
        void fetchSessionsRef.current();
        void fetchActivityRef.current();
      }
    }

    document.addEventListener('visibilitychange', handleVisibilityChange);

    return () => {
      clearTimers();
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [scheduleSessions, scheduleActivity, clearTimers]);

  // ---- Exposed surface -----------------------------------------------------

  return {
    sessions: store.sessions,
    activity: store.activity,
    isFetching: store.isFetching,
    lastError: store.lastError,
    selectedSessionId: store.selectedSessionId,
    sessionDetail: store.sessionDetail,
    filter: store.filter,
    fetchSessions: store.fetchSessions,
    fetchActivity: store.fetchActivity,
    setFilter: store.setFilter,
    clearFilter: store.clearFilter,
    selectSession: store.selectSession,
  };
}
