/**
 * AgentSessionCard — displays one agent session with status, model, peers, and sparkline.
 *
 * Status dot semantics:
 *   green  — disconnected_at === null AND last_seen_at within last 60s
 *   amber  — disconnected_at === null but last_seen_at older than 60s (stale)
 *   grey   — disconnected_at is set
 *
 * The whole card is a button — clicking opens the SessionDetailDrawer.
 */

import { useMemo } from 'react';
import { Trash2 } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { ProjectPill } from '@/components/memory/ProjectPill';
import { Sparkline } from '@/components/agents/Sparkline';
import { connectedDuration, shortTime } from '@/lib/timeFormat';
import type { AgentSession, ToolCall } from '@/types/agents';

// ---------------------------------------------------------------------------
// Status helpers
// ---------------------------------------------------------------------------

type SessionStatus = 'active' | 'stale' | 'disconnected';

function getSessionStatus(session: AgentSession): SessionStatus {
  if (session.disconnected_at !== null) return 'disconnected';
  const lastSeen = new Date(session.last_seen_at).getTime();
  const staleThreshold = Date.now() - 60_000;
  return lastSeen >= staleThreshold ? 'active' : 'stale';
}

const STATUS_DOT: Record<SessionStatus, string> = {
  // Sage = alive (with the gentle hearth-pulse on active sessions)
  active: 'bg-sage animate-hearth-pulse',
  // Amber = idle/stale; theme-aware so light/dark both read correctly
  stale: 'bg-amber-500 dark:bg-amber-400',
  disconnected: 'bg-muted-foreground/40',
};

const STATUS_LABEL: Record<SessionStatus, string> = {
  active: 'Active',
  stale: 'Stale',
  disconnected: 'Disconnected',
};

// ---------------------------------------------------------------------------
// Sparkline data builder
// ---------------------------------------------------------------------------

/**
 * Bucket tool calls from `recent_tool_calls` into the last 10 minute-buckets.
 * Returns an array of 10 counts (oldest → newest).
 */
function buildSparklineValues(toolCalls: ToolCall[]): number[] {
  const now = Date.now();
  const buckets = Array.from<number>({ length: 10 }).fill(0);

  for (const call of toolCalls) {
    const age = now - new Date(call.called_at).getTime();
    const minuteIdx = Math.floor(age / 60_000); // 0 = most recent minute
    if (minuteIdx >= 0 && minuteIdx < 10) {
      // Reverse: bucket 0 = last minute (index 9 in array, newest on right)
      const arrIdx = 9 - minuteIdx;
      if (arrIdx >= 0 && arrIdx < 10) {
        buckets[arrIdx] = (buckets[arrIdx] ?? 0) + 1;
      }
    }
  }

  return buckets;
}

// ---------------------------------------------------------------------------
// Peer MCP chips
// ---------------------------------------------------------------------------

const MAX_VISIBLE_MCPS = 5;

interface PeerMcpChipsProps {
  peers: string[];
}

function PeerMcpChips({ peers }: PeerMcpChipsProps) {
  // Filter out "potluck" — we already know that one
  const filtered = peers.filter((p) => p.toLowerCase() !== 'potluck');

  if (filtered.length === 0) {
    return <span className="text-xs text-muted-foreground/60 italic">(unknown)</span>;
  }

  const visible = filtered.slice(0, MAX_VISIBLE_MCPS);
  const overflow = filtered.length - visible.length;

  return (
    <div className="flex flex-wrap gap-1">
      {visible.map((mcp) => (
        <span
          key={mcp}
          className="inline-flex items-center rounded-full bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-secondary-foreground"
        >
          {mcp}
        </span>
      ))}
      {overflow > 0 && (
        <span className="inline-flex items-center rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
          +{overflow} more
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Model badge
// ---------------------------------------------------------------------------

interface ModelBadgeProps {
  model: string | null;
  modelSource: AgentSession['model_source'];
}

function ModelBadge({ model, modelSource }: ModelBadgeProps) {
  if (model === null) {
    return (
      <span
        className="text-xs italic text-muted-foreground/50"
        title="This client didn't declare its model in the MCP handshake."
      >
        Model not reported
      </span>
    );
  }

  const sourceLabel = modelSource === 'self-reported' ? 'self-reported' : 'inferred';
  const sourceTooltip =
    modelSource === 'self-reported'
      ? 'The client explicitly declared its model in the MCP handshake.'
      : 'Model was inferred from request patterns or config files, not guaranteed.';

  return (
    <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <span className="font-mono text-xs text-foreground">{model}</span>
      {modelSource !== null && (
        <span
          title={sourceTooltip}
          className="inline-flex items-center rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground cursor-default"
        >
          {sourceLabel}
        </span>
      )}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Card component
// ---------------------------------------------------------------------------

interface AgentSessionCardProps {
  session: AgentSession;
  recentToolCalls?: ToolCall[];
  isSelected?: boolean;
  onSelect: (id: string) => void;
  /** When provided, a delete-button affordance appears on hover. Called with
   *  the session id; the parent owns the API call + list refresh. */
  onDelete?: (id: string) => void;
}

export function AgentSessionCard({
  session,
  recentToolCalls = [],
  isSelected = false,
  onSelect,
  onDelete,
}: AgentSessionCardProps) {
  const status = getSessionStatus(session);
  const sparkValues = useMemo(() => buildSparklineValues(recentToolCalls), [recentToolCalls]);

  const clientLabel = session.client_name ?? 'unknown agent';
  const footer =
    session.disconnected_at !== null
      ? `Disconnected at ${shortTime(session.disconnected_at)}`
      : connectedDuration(session.started_at);

  // Wrap in a relative div so the absolutely-positioned trash button can
  // live as a sibling of the main button (avoids the nested-button HTML
  // invalidity while still letting `group-hover:` reveal the trash icon).
  return (
    <div className="group relative">
    <button
      type="button"
      onClick={() => onSelect(session.id)}
      data-testid="agent-session-card"
      data-client={session.client_name ?? 'unknown'}
      aria-label={`Agent session: ${clientLabel}${session.project ? `, project ${session.project}` : ''}`}
      aria-pressed={isSelected}
      className={cn(
        'relative flex w-full flex-col gap-2 rounded-xl border bg-card p-4 text-left',
        // Hover: hairline lifts + ember-tinted bg. Selected: ember inset ring
        // (uses the design-system "ember-inset" shadow token).
        'transition-all duration-fast',
        'hover:shadow-hearth-sm hover:border-border',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        isSelected
          ? 'border-primary/45 bg-primary/5 shadow-ember-inset'
          : 'border-border/60',
      )}
    >
      {/* Row 1: status dot + client name + version + friendly-name + project pill.
          The friendly_name chip disambiguates simultaneous instances of the
          same client (two Claude Codes side-by-side). Falls back to omitting
          the chip for legacy sessions written before the migration. */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span
            className={cn('mt-0.5 h-2 w-2 shrink-0 rounded-full', STATUS_DOT[status])}
            aria-label={STATUS_LABEL[status]}
            title={STATUS_LABEL[status]}
          />
          <span className="truncate text-sm font-semibold text-foreground">{clientLabel}</span>
          {session.client_version !== null && (
            <span className="shrink-0 text-xs text-muted-foreground">v{session.client_version}</span>
          )}
          {session.friendly_name !== null && (
            <span
              className="shrink-0 rounded-md border border-border/40 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground/80"
              title="Auto-assigned nickname for this session, distinct per process"
            >
              {session.friendly_name}
            </span>
          )}
        </div>
        <ProjectPill project={session.project} className="shrink-0" />
      </div>

      {/* Row 2: model */}
      <ModelBadge model={session.model} modelSource={session.model_source} />

      {/* Row 3: peer MCPs */}
      <div className="flex items-center gap-1.5">
        <span className="shrink-0 font-serif text-[10px] italic text-muted-foreground/60 tracking-hearth-label">
          peers
        </span>
        <PeerMcpChips peers={session.peer_mcps} />
      </div>

      {/* Row 4: tool call count + sparkline */}
      <div className="flex items-center justify-between gap-4">
        <span className="text-xs text-muted-foreground">
          <span className="font-medium text-foreground">{session.tool_call_count}</span> tool calls
        </span>
        <div className="text-primary/70">
          <Sparkline values={sparkValues} width={80} height={26} />
        </div>
      </div>

      {/* Row 5: footer */}
      <p className="text-[11px] text-muted-foreground/70">{footer}</p>
    </button>
    {onDelete && (
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onDelete(session.id);
        }}
        aria-label="Delete this session"
        title="Delete session"
        className={cn(
          'absolute right-2 top-2 flex h-7 w-7 items-center justify-center rounded-md',
          'text-muted-foreground/60 hover:text-destructive hover:bg-destructive/10',
          'opacity-0 group-hover:opacity-100 focus-visible:opacity-100',
          'transition-opacity duration-fast',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        )}
      >
        <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    )}
    </div>
  );
}
