/**
 * AgentsView — /agents route.
 *
 * Shows connected agent sessions and a real-time activity feed.
 *
 * Layout (left to right when drawer is open):
 *   [main content] [SessionDetailDrawer]
 *
 * The drawer pushes content rather than overlaying it.
 *
 * "Active now" = disconnected_at === null AND last_seen_at within last 60s.
 * "Stale" = disconnected_at === null but last_seen_at older than 60s.
 *
 * New activity rows arrive prepended from the store; we track their IDs
 * for the flash animation that fades over 1 second.
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { Activity } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { useAgents } from '@/hooks/useAgents';
import { AgentSessionCard } from '@/components/agents/AgentSessionCard';
import { ActivityFeed } from '@/components/agents/ActivityFeed';
import { SessionDetailDrawer } from '@/components/agents/SessionDetailDrawer';
import type { AgentSession } from '@/types/agents';

// ---------------------------------------------------------------------------
// Session status helpers (duplicated from AgentSessionCard to avoid coupling)
// ---------------------------------------------------------------------------

function isActive(session: AgentSession): boolean {
  if (session.disconnected_at !== null) return false;
  return new Date(session.last_seen_at).getTime() >= Date.now() - 60_000;
}

function isDisconnected(session: AgentSession): boolean {
  return session.disconnected_at !== null;
}

// ---------------------------------------------------------------------------
// AgentsView
// ---------------------------------------------------------------------------

export function AgentsView() {
  const {
    sessions,
    activity,
    isFetching,
    selectedSessionId,
    sessionDetail,
    filter,
    setFilter,
    clearFilter,
    selectSession,
  } = useAgents();

  const [showDisconnected, setShowDisconnected] = useState(false);

  // Track IDs of calls that arrived in the last fetch cycle so we can flash them
  const prevActivityIdsRef = useRef<Set<string>>(new Set());
  const [newCallIds, setNewCallIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    const currentIds = new Set(activity.map((c) => c.id));
    const fresh = new Set<string>();
    for (const id of currentIds) {
      if (!prevActivityIdsRef.current.has(id)) {
        fresh.add(id);
      }
    }
    prevActivityIdsRef.current = currentIds;
    if (fresh.size > 0) {
      setNewCallIds(fresh);
      // Clear the highlight set after 1.2s (slightly past the 1s animation)
      const timer = setTimeout(() => setNewCallIds(new Set()), 1200);
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [activity]);

  // Refs map for session cards so we can scroll a card into view from ActivityFeed clicks
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  const handleSelectSession = useCallback(
    (id: string) => {
      // Toggle: clicking the already-selected session closes the drawer
      if (id === selectedSessionId) {
        selectSession(null);
        return;
      }
      selectSession(id);
      // Scroll the card into view (best-effort)
      const cardEl = cardRefs.current.get(id);
      if (cardEl !== undefined) {
        cardEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    },
    [selectedSessionId, selectSession],
  );

  const handleCloseDrawer = useCallback(() => {
    selectSession(null);
  }, [selectSession]);

  // Derived counts
  const activeSessions = sessions.filter(isActive);
  const disconnectedSessions = sessions.filter(isDisconnected);

  const visibleSessions = showDisconnected
    ? sessions
    : sessions.filter((s) => !isDisconnected(s));

  const hasEverHadSessions = sessions.length > 0;

  // ---------------------------------------------------------------------------
  // Empty state (no sessions ever)
  // ---------------------------------------------------------------------------

  if (!isFetching && !hasEverHadSessions) {
    return (
      <div className="flex h-full flex-col">
        <ViewHeader isFetching={isFetching} />
        <div className="flex flex-1 items-center justify-center p-8 text-center">
          <div className="max-w-sm space-y-3">
            <Activity className="mx-auto h-12 w-12 text-muted-foreground/30" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-foreground">No agent sessions yet</h2>
            <p className="text-xs text-muted-foreground leading-relaxed">
              Configure Potluck as an MCP server in Claude Code, Cursor, Zed, or Windsurf — see{' '}
              <span className="font-medium text-foreground">Settings → Advanced → External agents (MCP)</span>.
            </p>
          </div>
        </div>
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // Normal view
  // ---------------------------------------------------------------------------

  return (
    <div className="flex h-full overflow-hidden">
      {/* Main content */}
      <div className="flex flex-1 min-w-0 flex-col overflow-hidden">
        {/* Header */}
        <ViewHeader isFetching={isFetching}>
          {/* Stats + toggle */}
          <div className="flex items-center gap-4">
            <span className="text-xs text-muted-foreground">
              <span className="font-medium text-foreground">{activeSessions.length}</span> active
              {disconnectedSessions.length > 0 && (
                <>
                  {' · '}
                  <span className="font-medium text-foreground">{disconnectedSessions.length}</span> disconnected
                </>
              )}
            </span>

            {disconnectedSessions.length > 0 && (
              <label className="flex cursor-pointer items-center gap-1.5 select-none">
                <input
                  type="checkbox"
                  checked={showDisconnected}
                  onChange={(e) => setShowDisconnected(e.target.checked)}
                  className="h-3.5 w-3.5 rounded border-input accent-primary"
                  aria-label="Show disconnected sessions"
                />
                <span className="text-xs text-muted-foreground">Show disconnected</span>
              </label>
            )}
          </div>
        </ViewHeader>

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-8">
          {/* ACTIVE NOW section */}
          <section aria-label="Active sessions">
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Active Now
            </h2>

            {visibleSessions.length === 0 ? (
              <p className="text-sm text-muted-foreground/70">
                {showDisconnected ? 'No sessions recorded.' : 'No active or stale sessions.'}
              </p>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {visibleSessions.map((session) => (
                  <div
                    key={session.id}
                    ref={(el) => {
                      if (el !== null) {
                        cardRefs.current.set(session.id, el);
                      } else {
                        cardRefs.current.delete(session.id);
                      }
                    }}
                  >
                    <AgentSessionCard
                      session={session}
                      recentToolCalls={
                        sessionDetail?.id === session.id
                          ? sessionDetail.recent_tool_calls
                          : activity.filter((c) => c.session_id === session.id)
                      }
                      isSelected={session.id === selectedSessionId}
                      onSelect={handleSelectSession}
                    />
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* RECENT ACTIVITY section */}
          <ActivityFeed
            sessions={sessions}
            activity={activity}
            selectedSessionId={selectedSessionId}
            filter={filter}
            onSetFilter={setFilter}
            onClearFilter={clearFilter}
            onSelectSession={handleSelectSession}
            newCallIds={newCallIds}
          />
        </div>
      </div>

      {/* Session detail drawer — pushes content */}
      <SessionDetailDrawer
        detail={selectedSessionId !== null ? sessionDetail : null}
        onClose={handleCloseDrawer}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// ViewHeader
// ---------------------------------------------------------------------------

interface ViewHeaderProps {
  isFetching: boolean;
  children?: React.ReactNode;
}

function ViewHeader({ isFetching, children }: ViewHeaderProps) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-6 py-4">
      <div className="flex items-center gap-3">
        <Activity className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
        <h1 className="text-sm font-semibold">Agents</h1>
        {isFetching && (
          <div
            className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-primary border-t-transparent"
            role="status"
            aria-label="Loading agent data"
          />
        )}
      </div>
      {children !== undefined && <div>{children}</div>}
    </div>
  );
}
