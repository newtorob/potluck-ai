/**
 * AgentsView - /agents route.
 *
 * Shows connected agent sessions and a real-time activity feed.
 *
 * Layout (left to right when drawer is open):
 *   [main content] [SessionDetailDrawer]
 *
 * The drawer pushes content rather than overlaying it.
 *
 * "Active now" = disconnected_at === null and last_seen_at within last 60s.
 * "Stale" = disconnected_at === null but last_seen_at older than 60s.
 *
 * New activity rows arrive prepended from the store; we track their IDs
 * for the flash animation that fades over 1 second.
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { Activity, Cable, GitBranch, Trash2 } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import { useAgents } from '@/hooks/useAgents';
import { AgentSessionCard } from '@/components/agents/AgentSessionCard';
import { ActivityFeed } from '@/components/agents/ActivityFeed';
import { SessionDetailDrawer } from '@/components/agents/SessionDetailDrawer';
import { AgentRunsPanel } from '@/components/agents/AgentRunsPanel';
import { FEATURES } from '@/lib/features';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';
import type { AgentSession, ToolCall, SessionDetail } from '@/types/agents';

// ---------------------------------------------------------------------------
// Session status helpers - keyed off the server-computed `liveness` field so
// the four tiers (live / idle / stale / disconnected) match the backend's
// own time math. Pre-migration sessions without `liveness` fall back to the
// older client-side calc.
// ---------------------------------------------------------------------------

function liveness(session: AgentSession): 'live' | 'idle' | 'stale' | 'disconnected' {
  // Defensive: a sidecar that hasn't been bounced since the migration may
  // omit liveness. Recompute client-side in that case.
  if (session.liveness) return session.liveness;
  if (session.disconnected_at !== null) return 'disconnected';
  const age = (Date.now() - new Date(session.last_seen_at).getTime()) / 1000;
  if (age < 60) return 'live';
  if (age < 300) return 'idle';
  return 'stale';
}

function isDisconnected(session: AgentSession): boolean {
  return liveness(session) === 'disconnected';
}

// ---------------------------------------------------------------------------
// CountChip - colored-dot count for the AgentsView header. One per liveness
// tier. Declared above the main view so Vite's fast-refresh can't trip on
// a forward reference during HMR.
// ---------------------------------------------------------------------------

function CountChip({
  dotClass,
  count,
  label,
}: {
  dotClass: string;
  count: number;
  label: string;
}) {
  return (
    <span className="flex items-center gap-1">
      <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', dotClass)} aria-hidden="true" />
      <span className="font-medium text-foreground">{count}</span>
      <span>{label}</span>
    </span>
  );
}

type AgentsMode = 'runs' | 'connections';

function AgentsModeTabs({ mode, onChange }: { mode: AgentsMode; onChange: (mode: AgentsMode) => void }) {
  return (
    <div role="tablist" aria-label="Agent observability mode" className="flex rounded-lg border border-border bg-muted p-0.5">
      <button
        type="button"
        role="tab"
        id="agents-tab-runs"
        aria-selected={mode === 'runs'}
        aria-controls="agents-panel-runs"
        onClick={() => onChange('runs')}
        className={cn(
          'flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium transition-colors duration-fast',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          mode === 'runs' ? 'bg-background text-foreground shadow-hearth-sm' : 'text-muted-foreground hover:text-foreground',
        )}
      >
        <GitBranch className="h-3 w-3" aria-hidden="true" />
        Runs
      </button>
      <button
        type="button"
        role="tab"
        id="agents-tab-connections"
        aria-selected={mode === 'connections'}
        aria-controls="agents-panel-connections"
        onClick={() => onChange('connections')}
        className={cn(
          'flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium transition-colors duration-fast',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          mode === 'connections' ? 'bg-background text-foreground shadow-hearth-sm' : 'text-muted-foreground hover:text-foreground',
        )}
      >
        <Cable className="h-3 w-3" aria-hidden="true" />
        Connections
      </button>
    </div>
  );
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

  const [activeMode, setActiveMode] = useState<AgentsMode>(FEATURES.agentWorkroom ? 'runs' : 'connections');
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

  // One-click cleanup: trigger the backend sweep (marks stale → disconnected
  // + deletes worthless rows), then clear all disconnected. Two API calls,
  // single refresh. This is the button people will actually use; the
  // narrower "clear disconnected" stays as a more conservative path.
  const handleCleanUpStale = useCallback(
    async () => {
      setIsClearing(true);
      try {
        const sweep = await localApi.agents.sweep();
        await localApi.agents.clearDisconnected();
        selectSession(null);
        await fetchSessions();
        // Informational console line; AgentsView is a power-user surface
        // so a toast isn't necessary, but the numbers are nice in devtools.
        console.info('agent cleanup:', sweep);
      } catch (err) {
        console.error('failed to clean up stale sessions', err);
      } finally {
        setIsClearing(false);
      }
    },
    [fetchSessions, selectSession],
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

  // Derived counts. Four tiers per the server's `liveness` field.
  const liveSessions = sessions.filter((s) => liveness(s) === 'live');
  const idleSessions = sessions.filter((s) => liveness(s) === 'idle');
  const staleSessions = sessions.filter((s) => liveness(s) === 'stale');
  const disconnectedSessions = sessions.filter(isDisconnected);

  const hasEverHadSessions = sessions.length > 0;

  if (FEATURES.agentWorkroom && activeMode === 'runs') {
    return (
      <div
        id="agents-panel-runs"
        role="tabpanel"
        aria-labelledby="agents-tab-runs"
        className="flex h-full flex-col overflow-hidden"
      >
        <AgentsHeader isFetching={false}>
          {FEATURES.agentWorkroom && <AgentsModeTabs mode={activeMode} onChange={setActiveMode} />}
        </AgentsHeader>
        <AgentRunsPanel />
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // Empty state (no sessions ever)
  // ---------------------------------------------------------------------------

  if (!isFetching && !hasEverHadSessions) {
    return (
      <div className="flex h-full flex-col">
        <AgentsHeader isFetching={isFetching}>
          {FEATURES.agentWorkroom && <AgentsModeTabs mode={activeMode} onChange={setActiveMode} />}
        </AgentsHeader>
        <EmptyState
          icon={Activity}
          title="No agent sessions yet"
          description={
            <>
              Configure Potluck as an MCP server in Claude Code, Cursor, Zed, or Windsurf. See{' '}
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
    <div
      id="agents-panel-connections"
      role="tabpanel"
      aria-labelledby="agents-tab-connections"
      className="flex h-full overflow-hidden"
      data-testid="agents-view"
    >
      {/* Main content */}
      <div className="flex flex-1 min-w-0 flex-col overflow-hidden">
        {/* Header */}
        <AgentsHeader isFetching={isFetching}>
          <div className="flex items-center gap-4">
            {FEATURES.agentWorkroom && <AgentsModeTabs mode={activeMode} onChange={setActiveMode} />}
            {/* Honest count by liveness tier. The old single "X active"
                conflated four states and lied about reality (sessions that
                had not heartbeated in days still counted as "active"). Now
                we show each tier explicitly with a colored dot so the
                user can tell at a glance what's actually happening. */}
            <span className="flex items-center gap-2.5 text-xs text-muted-foreground">
              <CountChip dotClass="bg-emerald-400" count={liveSessions.length} label="connected" />
              {idleSessions.length > 0 && (
                <CountChip dotClass="bg-sky-400/70" count={idleSessions.length} label="idle" />
              )}
              {staleSessions.length > 0 && (
                <CountChip dotClass="bg-amber-400/70" count={staleSessions.length} label="stale" />
              )}
              {disconnectedSessions.length > 0 && (
                <CountChip dotClass="bg-muted-foreground/40" count={disconnectedSessions.length} label="disconnected" />
              )}
            </span>

            {/* Clean-up stale: visible whenever there's at least one session,
                whether disconnected or not. Runs the backend sweep (marks stale
                + deletes worthless never-identified rows), then clears every
                disconnected row in one round-trip. The narrower "Clear N
                disconnected" stays as a more conservative path users can pick
                if they want to see what would be removed first. */}
            {sessions.length > 0 && (
              <button
                type="button"
                onClick={() => void handleCleanUpStale()}
                disabled={isClearing}
                className={cn(
                  'flex items-center gap-1 rounded-md border border-border/60 px-2 py-1 text-xs',
                  'text-muted-foreground hover:text-foreground hover:border-border',
                  'transition-colors duration-fast',
                  'disabled:cursor-not-allowed disabled:opacity-50',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                )}
                aria-label="Sweep stale sessions and clear disconnected"
                title="Mark sessions with no recent activity as disconnected, drop sessions that never identified themselves, and remove every disconnected row"
              >
                <Trash2 className="h-3 w-3" aria-hidden="true" />
                <span>{isClearing ? 'Cleaning…' : 'Clean up stale'}</span>
              </button>
            )}

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
          {/* CONNECTED NOW section - sessions with heartbeat in last 60s.
              This is what "my one agent" means colloquially; it's the
              actually-alive process you're working with right now. */}
          <AgentSessionGroup
            label="Connected now"
            sessions={liveSessions}
            emptyHint={
              (idleSessions.length + staleSessions.length + (showDisconnected ? disconnectedSessions.length : 0)) === 0
                ? 'No agent is currently connected.'
                : null
            }
            activity={activity}
            sessionDetail={sessionDetail}
            selectedSessionId={selectedSessionId}
            cardRefs={cardRefs}
            onSelect={handleSelectSession}
            onDelete={(id) => void handleDeleteSession(id)}
          />

          {/* IDLE section - connected but quiet for 1–5 min. Same agent,
              just not invoking a tool right this second. Common during
              user-think pauses. */}
          {idleSessions.length > 0 && (
            <AgentSessionGroup
              label="Idle"
              hint="connected, quiet for 1–5 minutes"
              sessions={idleSessions}
              activity={activity}
              sessionDetail={sessionDetail}
              selectedSessionId={selectedSessionId}
              cardRefs={cardRefs}
              onSelect={handleSelectSession}
              onDelete={(id) => void handleDeleteSession(id)}
            />
          )}

          {/* STALE section - last seen >5 min ago, sweep will mark them
              disconnected on its next tick. Surfacing them gives users a
              chance to see what's about to be cleaned up. */}
          {staleSessions.length > 0 && (
            <AgentSessionGroup
              label="Stale"
              hint="no heartbeat for 5+ minutes · sweep will mark these disconnected"
              sessions={staleSessions}
              activity={activity}
              sessionDetail={sessionDetail}
              selectedSessionId={selectedSessionId}
              cardRefs={cardRefs}
              onSelect={handleSelectSession}
              onDelete={(id) => void handleDeleteSession(id)}
            />
          )}

          {/* DISCONNECTED section - gated by the toolbar checkbox. */}
          {showDisconnected && disconnectedSessions.length > 0 && (
            <AgentSessionGroup
              label="Disconnected"
              hint="closed cleanly or swept after going stale"
              sessions={disconnectedSessions}
              activity={activity}
              sessionDetail={sessionDetail}
              selectedSessionId={selectedSessionId}
              cardRefs={cardRefs}
              onSelect={handleSelectSession}
              onDelete={(id) => void handleDeleteSession(id)}
            />
          )}

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

      {/* Session detail drawer - pushes content */}
      <SessionDetailDrawer
        detail={selectedSessionId !== null ? sessionDetail : null}
        onClose={handleCloseDrawer}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// AgentsHeader - local helper that wraps the shared ViewHeader to surface
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

// ---------------------------------------------------------------------------
// AgentSessionGroup - one liveness-tier section of the AgentsView. Renders
// a 3-column grid of cards (sm:2, lg:3 cards across), or a single explanatory
// line when the group is empty (only used by "Connected now"). Other groups
// don't render at all when empty so the page stays compact.
// ---------------------------------------------------------------------------

interface AgentSessionGroupProps {
  label: string;
  /** Optional subtle text beneath the section label, explaining the tier. */
  hint?: string;
  /** Only rendered when `sessions` is empty - explains why nothing's here. */
  emptyHint?: string | null;
  sessions: AgentSession[];
  activity: ToolCall[];
  sessionDetail: SessionDetail | null;
  selectedSessionId: string | null;
  cardRefs: React.MutableRefObject<Map<string, HTMLDivElement>>;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
}

function AgentSessionGroup({
  label,
  hint,
  emptyHint,
  sessions,
  activity,
  sessionDetail,
  selectedSessionId,
  cardRefs,
  onSelect,
  onDelete,
}: AgentSessionGroupProps) {
  return (
    <section aria-label={label}>
      <div className="mb-3 flex items-baseline gap-2">
        <h2 className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
          {label}
        </h2>
        {sessions.length > 0 && (
          <span className="text-[11px] text-muted-foreground/60">
            ({sessions.length})
          </span>
        )}
        {hint !== undefined && (
          <span className="text-[11px] text-muted-foreground/50">{hint}</span>
        )}
      </div>

      {sessions.length === 0 ? (
        emptyHint !== undefined && emptyHint !== null ? (
          <p className="text-sm text-muted-foreground/70">{emptyHint}</p>
        ) : null
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {sessions.map((session) => (
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
                onSelect={onSelect}
                onDelete={onDelete}
              />
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
