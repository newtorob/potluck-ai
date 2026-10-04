import { useCallback, useEffect, useRef } from 'react';
import { useAgentRunStore } from '@/stores/agentRunStore';

const RUN_LIST_INTERVAL_MS = 3_000;
const LIVE_EVENT_INTERVAL_MS = 750;

export function useAgentRuns() {
  const store = useAgentRunStore();
  const fetchRunsRef = useRef(store.fetchRuns);
  const pollSelectedRunRef = useRef(store.pollSelectedRun);
  fetchRunsRef.current = store.fetchRuns;
  pollSelectedRunRef.current = store.pollSelectedRun;

  useEffect(() => {
    void fetchRunsRef.current();
  }, []);

  const runsTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const eventsTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const scheduleRuns = useCallback(() => {
    runsTimerRef.current = setTimeout(() => {
      if (!document.hidden) void fetchRunsRef.current();
      scheduleRuns();
    }, RUN_LIST_INTERVAL_MS);
  }, []);

  const scheduleEvents = useCallback(() => {
    eventsTimerRef.current = setTimeout(() => {
      if (!document.hidden) void pollSelectedRunRef.current();
      scheduleEvents();
    }, LIVE_EVENT_INTERVAL_MS);
  }, []);

  useEffect(() => {
    scheduleRuns();
    scheduleEvents();
    const onVisibilityChange = () => {
      if (!document.hidden) {
        void fetchRunsRef.current();
        void pollSelectedRunRef.current();
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      if (runsTimerRef.current !== null) clearTimeout(runsTimerRef.current);
      if (eventsTimerRef.current !== null) clearTimeout(eventsTimerRef.current);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [scheduleEvents, scheduleRuns]);

  return store;
}
