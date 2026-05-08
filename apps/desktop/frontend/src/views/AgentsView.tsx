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
import { Activity, Trash2 } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import { useAgents } from '@/hooks/useAgents';
import { AgentSessionCard } from '@/components/agents/AgentSessionCard';
import { ActivityFeed } from '@/components/agents/ActivityFeed';
import { SessionDetailDrawer } from '@/components/agents/SessionDetailDrawer';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';
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
    fetchSessions,
  } = useAgents();

  const [showDisconnected, setShowDisconnected] = useState(false);
  const [isClearing, setIsClearing] = useState(false);

  // ---- Delete handlers ------------------------------------------------------
  // Per-session delete: called from a trash icon on each AgentSessionCard.
  // Bulk clear-disconnected: called from the header chip when there's at
  // least one disconnected session in view.
  // Both refresh the sessions list after the API call so the UI reflects
  // the database immediately, without waiting for the 5s poll cycle.
  const handleDeleteSession = useCallback(
    async (id: string) => {
      if (!confirm('Delete this session row? The agent itself is unaffected and will re-register if it makes another call.')) return;
      try {
        await localApi.agents.deleteSession(id);
        if (id === selectedSessionId) selectSession(null);
        await fetchSessions();
      } catch (err) {
        console.error('failed to delete session', err);
      }
    },
    [fetchSessions, selectedSessionId, selectSession],
  );

  const handleClearDisconnected = useCallback(
    async () => {
      const disconnected = sessions.filter((s) => s.disconnected_at !== null);
      if (disconnected.length === 0) return;
      if (!confirm(`Remove ${disconnected.length} disconnected session row${disconnected.length === 1 ? '' : 's'}? Active sessions are not touched.`)) return;
      setIsClearing(true);
      try {
        await localApi.agents.clearDisconnected();
        if (selectedSessionId !== null && disconnected.some((s) => s.id === selectedSessionId)) {
          selectSession(null);
        }
        await fetchSessions();
      } catch (err) {
        console.error('failed to clear disconnected sessions', err);
      } finally {
        setIsClearing(false);
      }
    },
    [sessions, fetchSessions, selectedSessionId, selectSession],
  );

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
        <AgentsHeader isFetching={isFetching} />
        <EmptyState
          icon={Activity}
          title="No agent sessions yet"
          description={
            <>
              Configure Potluck as an MCP server in Claude Code, Cursor, Zed, or Windsurf — see{' '}
              <span className="font-medium text-foreground">Settings → Advanced → External agents (MCP)</span>.
            </>
          }
        />
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
        <AgentsHeader isFetching={isFetching}>
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
              <>
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

                <button
                  type="button"
                  onClick={() => void handleClearDisconnected()}
                  disabled={isClearing}
                  className={cn(
                    'flex items-center gap-1 rounded-md border border-border/60 px-2 py-1 text-xs',
                    'text-muted-foreground hover:text-destructive hover:border-destructive/30 hover:bg-destructive/5',
                    'transition-colors duration-fast',
                    'disabled:cursor-not-allowed disabled:opacity-50',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  )}
                  aria-label="Clear all disconnected sessions"
                  title="Remove disconnected session rows"
                >
                  <Trash2 className="h-3 w-3" aria-hidden="true" />
                  <span>{isClearing ? 'Clearing…' : `Clear ${disconnectedSessions.length} disconnected`}</span>
                </button>
              </>
            )}
          </div>
        </AgentsHeader>

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-8">
          {/* ACTIVE NOW section */}
          <section aria-label="Active sessions">
            <h2 className="mb-3 font-serif text-[11px] italic text-muted-foreground" style={{ letterSpacing: '0.04em' }}>
              Active now
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
                      onDelete={(id) => void handleDeleteSession(id)}
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
// AgentsHeader — local helper that wraps the shared ViewHeader to surface
// the spinner-while-fetching affordance the original local helper provided.
// ---------------------------------------------------------------------------

interface AgentsHeaderProps {
  isFetching: boolean;
  children?: React.ReactNode;
}

function AgentsHeader({ isFetching, children }: AgentsHeaderProps) {
  return (
    <ViewHeader
      title={
        <span className="flex items-center gap-3">
          Agents
          {isFetching && (
            <span
              className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-primary border-t-transparent"
              role="status"
              aria-label="Loading agent data"
            />
          )}
        </span>
      }
      tools={children}
    />
  );
}
