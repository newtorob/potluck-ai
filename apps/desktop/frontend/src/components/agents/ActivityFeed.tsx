/**
 * ActivityFeed — time-ordered list of recent tool calls with filter controls.
 *
 * Each row shows: time-ago · client · tool · args (safe metadata keys) · results · duration · error
 *
 * Args rendering:
 *   Only surfaces these safe metadata fields from args_summary:
 *     category, project, limit, status, query (truncated to 30 chars), tool
 *   All other keys (prompt content, raw text, embeddings) are skipped.
 *   Null/undefined values are skipped.
 *
 * Row highlighting:
 *   Clicking a row calls onSelectSession(session_id). The session card above
 *   is scrolled into view by the parent AgentsView via a ref map.
 *
 * Live time updates: a 5-second interval re-renders relative timestamps.
 */

import { useEffect, useState, useId } from 'react';
import { Bot } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { relativeTime } from '@/lib/timeFormat';
import type { ToolCall, AgentSession } from '@/types/agents';

// ---------------------------------------------------------------------------
// Safe args fields to display
// ---------------------------------------------------------------------------

const SAFE_ARG_KEYS = ['category', 'project', 'limit', 'status', 'query', 'tool'] as const;

function renderArgsSummary(args: Record<string, unknown> | null): string | null {
  if (args === null) return null;
  const parts: string[] = [];
  for (const key of SAFE_ARG_KEYS) {
    const val = args[key as string];
    if (val === null || val === undefined) continue;
    let display = String(val);
    // Truncate long query strings to keep the row compact
    if (key === 'query' && display.length > 30) {
      display = `${display.slice(0, 30)}…`;
    }
    parts.push(`${key}=${display}`);
  }
  return parts.length > 0 ? parts.join(' · ') : null;
}

// ---------------------------------------------------------------------------
// Client icon
// ---------------------------------------------------------------------------

const CLIENT_ICON_LABEL: Record<string, string> = {
  'claude-code': 'CC',
  'cursor': 'Cu',
  'zed': 'Zd',
  'windsurf': 'Ws',
  'potluck-chat': 'PL',
};

interface ClientIconProps {
  clientName: string | null;
}

function ClientIcon({ clientName }: ClientIconProps) {
  const label = clientName !== null ? (CLIENT_ICON_LABEL[clientName] ?? null) : null;
  if (label === null) {
    return <Bot className="h-3 w-3 shrink-0 text-muted-foreground/60" aria-hidden="true" />;
  }
  return (
    <span className="inline-flex h-4 w-6 shrink-0 items-center justify-center rounded bg-muted text-[9px] font-bold text-muted-foreground">
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Single activity row
// ---------------------------------------------------------------------------

interface ActivityRowProps {
  call: ToolCall;
  clientName: string | null;
  isHighlighted: boolean;
  isNew: boolean;
  onSelect: (sessionId: string) => void;
}

function ActivityRow({ call, clientName, isHighlighted, isNew, onSelect }: ActivityRowProps) {
  const argsSummary = renderArgsSummary(call.args_summary);

  return (
    <li
      className={cn(
        'group flex items-start gap-2 rounded-lg px-3 py-2 text-xs transition-colors',
        'cursor-pointer hover:bg-accent/40',
        isHighlighted && 'bg-primary/10',
        isNew && 'animate-[fadeHighlight_1s_ease-out_forwards]',
      )}
      onClick={() => onSelect(call.session_id)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onSelect(call.session_id);
        }
      }}
      aria-label={`Tool call: ${call.tool_name} by ${clientName ?? 'unknown'}`}
    >
      {/* Time */}
      <span className="w-12 shrink-0 text-muted-foreground/70">{relativeTime(call.called_at)}</span>

      {/* Client */}
      <div className="flex shrink-0 items-center gap-1">
        <ClientIcon clientName={clientName} />
        <span className="text-muted-foreground">{clientName ?? 'unknown'}</span>
      </div>

      {/* Separator */}
      <span className="shrink-0 text-muted-foreground/30">·</span>

      {/* Tool name */}
      <span className="shrink-0 font-mono text-foreground">{call.tool_name}</span>

      {/* Args summary */}
      {argsSummary !== null && (
        <>
          <span className="shrink-0 text-muted-foreground/30">·</span>
          <span className="min-w-0 truncate text-muted-foreground">{argsSummary}</span>
        </>
      )}

      {/* Spacer */}
      <span className="flex-1" />

      {/* Result count */}
      {call.result_count !== null && (
        <span className="shrink-0 text-sage">{call.result_count} results</span>
      )}

      {/* Duration */}
      {call.duration_ms !== null && call.error === null && (
        <span className="shrink-0 text-muted-foreground/60">{call.duration_ms}ms</span>
      )}

      {/* Error */}
      {call.error !== null && (
        <span
          className="shrink-0 max-w-[140px] truncate text-destructive"
          title={call.error}
        >
          {call.error}
        </span>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Filter controls
// ---------------------------------------------------------------------------

interface FilterControlsProps {
  sessions: AgentSession[];
  activity: ToolCall[];
  currentFilter: { client?: string | undefined; project?: string | undefined; tool?: string | undefined };
  onSetFilter: (patch: { client?: string | undefined; project?: string | undefined; tool?: string | undefined }) => void;
  onClearFilter: () => void;
  selectId: string;
}

function FilterControls({
  sessions,
  activity,
  currentFilter,
  onSetFilter,
  onClearFilter,
  selectId,
}: FilterControlsProps) {
  const knownClients = Array.from(
    new Set(sessions.map((s) => s.client_name).filter((c): c is string => c !== null)),
  );
  const knownProjects = Array.from(
    new Set(sessions.map((s) => s.project).filter((p): p is string => p !== null)),
  );
  const knownTools = Array.from(new Set(activity.map((c) => c.tool_name)));

  const hasFilter =
    (currentFilter.client ?? '') !== '' ||
    (currentFilter.project ?? '') !== '' ||
    (currentFilter.tool ?? '') !== '';

  return (
    <div className="flex flex-wrap items-center gap-2">
      {/* Client filter */}
      <div className="flex items-center gap-1">
        <label htmlFor={`${selectId}-client`} className="text-xs text-muted-foreground">
          Client
        </label>
        <select
          id={`${selectId}-client`}
          value={currentFilter.client ?? ''}
          onChange={(e) => onSetFilter({ client: e.target.value || undefined })}
          className={cn(
            'rounded-md border border-input bg-background px-2 py-1 text-xs text-foreground',
            'focus:outline-none focus:ring-2 focus:ring-ring',
          )}
        >
          <option value="">All</option>
          {knownClients.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>

      {/* Project filter */}
      <div className="flex items-center gap-1">
        <label htmlFor={`${selectId}-project`} className="text-xs text-muted-foreground">
          Project
        </label>
        <select
          id={`${selectId}-project`}
          value={currentFilter.project ?? ''}
          onChange={(e) => onSetFilter({ project: e.target.value || undefined })}
          className={cn(
            'rounded-md border border-input bg-background px-2 py-1 text-xs text-foreground',
            'focus:outline-none focus:ring-2 focus:ring-ring',
          )}
        >
          <option value="">All</option>
          <option value="__global__">Global</option>
          {knownProjects.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>

      {/* Tool filter */}
      <div className="flex items-center gap-1">
        <label htmlFor={`${selectId}-tool`} className="text-xs text-muted-foreground">
          Tool
        </label>
        <select
          id={`${selectId}-tool`}
          value={currentFilter.tool ?? ''}
          onChange={(e) => onSetFilter({ tool: e.target.value || undefined })}
          className={cn(
            'rounded-md border border-input bg-background px-2 py-1 text-xs text-foreground',
            'focus:outline-none focus:ring-2 focus:ring-ring',
          )}
        >
          <option value="">All</option>
          {knownTools.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>

      {/* Clear */}
      {hasFilter && (
        <button
          type="button"
          onClick={onClearFilter}
          className={cn(
            'rounded-md border border-border px-2 py-1 text-xs text-muted-foreground',
            'hover:bg-accent hover:text-foreground transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          Clear
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ActivityFeed
// ---------------------------------------------------------------------------

interface ActivityFeedProps {
  sessions: AgentSession[];
  activity: ToolCall[];
  selectedSessionId: string | null;
  filter: { client?: string | undefined; project?: string | undefined; tool?: string | undefined };
  onSetFilter: (patch: { client?: string | undefined; project?: string | undefined; tool?: string | undefined }) => void;
  onClearFilter: () => void;
  onSelectSession: (id: string) => void;
  /** IDs of calls that just arrived and should get the flash highlight */
  newCallIds: Set<string>;
}

export function ActivityFeed({
  sessions,
  activity,
  selectedSessionId,
  filter,
  onSetFilter,
  onClearFilter,
  onSelectSession,
  newCallIds,
}: ActivityFeedProps) {
  const selectId = useId();

  // Build a session_id → client_name lookup
  const sessionClientMap = new Map<string, string | null>(
    sessions.map((s) => [s.id, s.client_name]),
  );

  // Tick every 5 seconds to keep relative timestamps fresh
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 5_000);
    return () => clearInterval(id);
  }, []);

  const hasFilter =
    (filter.client ?? '') !== '' ||
    (filter.project ?? '') !== '' ||
    (filter.tool ?? '') !== '';

  return (
    <section aria-label="Recent activity">
      {/* Section header + filters */}
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
          Recent activity
        </h2>
        <FilterControls
          sessions={sessions}
          activity={activity}
          currentFilter={filter}
          onSetFilter={onSetFilter}
          onClearFilter={onClearFilter}
          selectId={selectId}
        />
      </div>

      {/* Feed */}
      {activity.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-10 text-center">
          <Bot className="h-8 w-8 text-muted-foreground/30" aria-hidden="true" />
          {hasFilter ? (
            <>
              <p className="text-sm text-muted-foreground">No tool calls match these filters</p>
              <button
                type="button"
                onClick={onClearFilter}
                className="text-xs text-primary hover:underline focus-visible:outline-none"
              >
                Clear filters
              </button>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">No tool calls recorded yet</p>
          )}
        </div>
      ) : (
        <ul className="space-y-0.5" aria-label="Tool call activity">
          {activity.map((call) => (
            <ActivityRow
              key={call.id}
              call={call}
              clientName={sessionClientMap.get(call.session_id) ?? null}
              isHighlighted={call.session_id === selectedSessionId}
              isNew={newCallIds.has(call.id)}
              onSelect={onSelectSession}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
