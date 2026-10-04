import { useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Activity,
  AlertCircle,
  Bot,
  Clock3,
  Database,
  FileText,
  GitBranch,
  ListTodo,
  Radio,
  ShieldAlert,
  Wrench,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { EmptyState } from '@/components/layout/EmptyState';
import { relativeTime, shortTime } from '@/lib/timeFormat';
import { useAgentRuns } from '@/hooks/useAgentRuns';
import type {
  AgentEvent,
  AgentInstance,
  AgentResource,
  AgentRun,
  AgentRunDetail,
} from '@/types/agents';

function formatDuration(ms: number): string {
  if (ms < 1_000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1_000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes}m ${Math.floor((ms % 60_000) / 1_000)}s`;
}

function runDuration(run: AgentRun): string {
  const end = run.ended_at === null ? Date.now() : new Date(run.ended_at).getTime();
  return formatDuration(Math.max(0, end - new Date(run.started_at).getTime()));
}

const EVENT_TONE: Record<string, string> = {
  failed: 'border-destructive/35 bg-destructive/10 text-destructive',
  waiting: 'border-amber-500/35 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  stopped: 'border-muted-foreground/30 bg-muted text-muted-foreground',
};

function eventTone(event: AgentEvent): string {
  const statusTone = EVENT_TONE[event.status];
  if (statusTone !== undefined) return statusTone;
  if (event.event_type.startsWith('tool.')) {
    return 'border-primary/35 bg-primary/10 text-primary';
  }
  if (event.event_type.startsWith('agent.')) {
    return 'border-sage/40 bg-sage/10 text-sage-foreground';
  }
  return 'border-border bg-surface-2 text-foreground';
}

function eventLabel(event: AgentEvent): string {
  if (event.tool_name !== null) return event.tool_name;
  return event.event_type
    .split('.')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function RunCard({
  run,
  selected,
  onSelect,
}: {
  run: AgentRun;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(run.id)}
      aria-pressed={selected}
      className={cn(
        'w-full rounded-lg border p-3 text-left transition-all duration-fast',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        selected
          ? 'border-primary/45 bg-primary/5 shadow-ember-inset'
          : 'border-border/60 bg-card hover:border-border hover:bg-accent/50',
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className={cn(
              'h-2 w-2 shrink-0 rounded-full',
              run.status === 'active' ? 'animate-hearth-pulse bg-sage' : 'bg-muted-foreground/40',
            )}
            aria-hidden="true"
          />
          <span className="truncate text-sm font-semibold capitalize">{run.provider}</span>
          {run.control_mode === 'managed' && (
            <span className="rounded-full border border-primary/20 bg-primary/5 px-1.5 py-0.5 text-[9px] font-medium text-primary">
              Managed
            </span>
          )}
        </div>
        <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
          {run.status === 'active' ? 'LIVE' : run.status.toUpperCase()}
        </span>
      </div>
      <p className="mt-1.5 truncate text-xs text-muted-foreground">
        {run.project ?? 'Unknown project'}
      </p>
      <div className="mt-2 flex items-center gap-3 text-[11px] text-muted-foreground/80">
        <span>{run.agent_count} agents</span>
        <span>{run.event_count} events</span>
        {run.error_count > 0 && <span className="text-destructive">{run.error_count} errors</span>}
      </div>
      <p className="mt-1.5 text-[10px] text-muted-foreground/60">
        {run.status === 'active' ? relativeTime(run.last_event_at) : runDuration(run)}
      </p>
    </button>
  );
}

interface TimelineItem {
  event: AgentEvent;
  startedAt: number;
  endedAt: number | null;
}

function timelineItems(events: AgentEvent[]): TimelineItem[] {
  const completions = new Map<string, AgentEvent>();
  const startedCorrelations = new Set<string>();
  for (const event of events) {
    if (event.event_type === 'tool.started' && event.correlation_id !== null) {
      startedCorrelations.add(event.correlation_id);
    }
    if (
      event.correlation_id !== null &&
      (event.event_type === 'tool.completed' || event.event_type === 'tool.failed')
    ) {
      completions.set(event.correlation_id, event);
    }
  }

  const items: TimelineItem[] = [];
  for (const event of events) {
    if (
      (event.event_type === 'tool.completed' || event.event_type === 'tool.failed') &&
      event.correlation_id !== null &&
      startedCorrelations.has(event.correlation_id)
    ) {
      continue;
    }
    const completion =
      event.event_type === 'tool.started' && event.correlation_id !== null
        ? completions.get(event.correlation_id)
        : undefined;
    items.push({
      event: completion ?? event,
      startedAt: new Date(event.occurred_at).getTime(),
      endedAt: completion === undefined ? null : new Date(completion.occurred_at).getTime(),
    });
  }
  return items;
}

function LaneLabel({
  instance,
  shared,
}: {
  instance?: AgentInstance | undefined;
  shared?: boolean | undefined;
}) {
  const active = instance?.status === 'active';
  return (
    <div className="flex h-14 w-44 shrink-0 items-center gap-2 border-r border-border/60 px-3">
      <span
        className={cn(
          'h-2 w-2 shrink-0 rounded-full',
          shared ? 'bg-muted-foreground/50' : active ? 'animate-hearth-pulse bg-sage' : 'bg-muted-foreground/35',
        )}
        aria-hidden="true"
      />
      <div className="min-w-0">
        <p className="truncate text-xs font-medium">
          {shared ? 'Shared activity' : instance?.label ?? 'Agent'}
        </p>
        <p className="truncate text-[10px] text-muted-foreground">
          {shared ? 'attribution unavailable' : instance?.kind}
        </p>
      </div>
    </div>
  );
}

function TimelineLane({
  instance,
  shared,
  events,
  start,
  span,
  selectedEventId,
  onSelectEvent,
}: {
  instance?: AgentInstance;
  shared?: boolean;
  events: AgentEvent[];
  start: number;
  span: number;
  selectedEventId: string | null;
  onSelectEvent: (event: AgentEvent) => void;
}) {
  const items = timelineItems(events);
  return (
    <div className="flex border-t border-border/50 first:border-t-0">
      <LaneLabel instance={instance} shared={shared} />
      <div className="relative h-14 flex-1 bg-[linear-gradient(to_right,hsl(var(--border)/0.35)_1px,transparent_1px)] bg-[size:25%_100%]">
        {items.map((item) => {
          const left = Math.max(0, Math.min(98, ((item.startedAt - start) / span) * 100));
          const width =
            item.endedAt === null
              ? 6
              : Math.max(1.2, Math.min(100 - left, ((item.endedAt - item.startedAt) / span) * 100));
          const isDuration = item.endedAt !== null;
          return (
            <button
              key={item.event.id}
              type="button"
              onClick={() => onSelectEvent(item.event)}
              title={`${eventLabel(item.event)} at ${shortTime(item.event.occurred_at)}`}
              aria-label={`Inspect ${eventLabel(item.event)}`}
              className={cn(
                'absolute top-1/2 -translate-y-1/2 overflow-hidden border text-left',
                'focus-visible:z-20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                isDuration ? 'h-7 rounded-md px-1.5 text-[10px] font-mono' : 'h-3 w-3 rounded-full',
                eventTone(item.event),
                selectedEventId === item.event.id && 'z-10 ring-2 ring-ring',
              )}
              style={
                isDuration
                  ? { left: `${left}%`, width: `${width}%`, minWidth: 24 }
                  : { left: `${left}%` }
              }
            >
              {isDuration && <span className="block truncate">{eventLabel(item.event)}</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function Timeline({
  detail,
  selectedEventId,
  onSelectEvent,
}: {
  detail: AgentRunDetail;
  selectedEventId: string | null;
  onSelectEvent: (event: AgentEvent) => void;
}) {
  const times = detail.events.map((event) => new Date(event.occurred_at).getTime());
  const start = Math.min(...times, Date.now());
  const end = Math.max(...times, start + 1_000);
  const span = Math.max(1_000, end - start);
  const hasShared = detail.events.some((event) => event.agent_instance_id === null);

  return (
    <div className="min-w-[840px]">
      <div className="flex h-10 border-b border-border/60 bg-surface-1">
        <div className="flex w-44 shrink-0 items-center border-r border-border/60 px-3 font-serif text-[11px] italic tracking-hearth-label text-muted-foreground">
          Agent tree
        </div>
        <div className="relative flex flex-1 items-center justify-between px-3 font-mono text-[9px] text-muted-foreground/60">
          {[0, 0.25, 0.5, 0.75, 1].map((tick) => (
            <span key={tick}>{formatDuration(Math.round(span * tick))}</span>
          ))}
        </div>
      </div>
      {detail.instances.map((instance) => (
        <TimelineLane
          key={instance.id}
          instance={instance}
          events={detail.events.filter((event) => event.agent_instance_id === instance.id)}
          start={start}
          span={span}
          selectedEventId={selectedEventId}
          onSelectEvent={onSelectEvent}
        />
      ))}
      {hasShared && (
        <TimelineLane
          shared
          events={detail.events.filter((event) => event.agent_instance_id === null)}
          start={start}
          span={span}
          selectedEventId={selectedEventId}
          onSelectEvent={onSelectEvent}
        />
      )}
    </div>
  );
}

function ResourceIcon({ kind }: { kind: AgentResource['kind'] }) {
  if (kind === 'file') return <FileText className="h-3.5 w-3.5" aria-hidden="true" />;
  if (kind === 'memory') return <Database className="h-3.5 w-3.5" aria-hidden="true" />;
  return <ListTodo className="h-3.5 w-3.5" aria-hidden="true" />;
}

function EventInspector({
  event,
  resources,
}: {
  event: AgentEvent | null;
  resources: AgentResource[];
}) {
  if (event === null) {
    return (
      <div className="flex flex-1 items-center justify-center p-6 text-center text-xs text-muted-foreground">
        Select an event to inspect its observable metadata.
      </div>
    );
  }
  return (
    <div className="flex-1 overflow-y-auto p-4">
      <div className="flex items-start gap-2">
        {event.event_type.startsWith('tool.') ? (
          <Wrench className="mt-0.5 h-4 w-4 text-primary" aria-hidden="true" />
        ) : event.status === 'failed' ? (
          <AlertCircle className="mt-0.5 h-4 w-4 text-destructive" aria-hidden="true" />
        ) : (
          <Activity className="mt-0.5 h-4 w-4 text-muted-foreground" aria-hidden="true" />
        )}
        <div className="min-w-0">
          <h3 className="break-words text-sm font-semibold">{eventLabel(event)}</h3>
          <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">{event.event_type}</p>
        </div>
      </div>

      <dl className="mt-5 space-y-3 text-xs">
        <div>
          <dt className="text-muted-foreground">Status</dt>
          <dd className="mt-0.5 capitalize text-foreground">{event.status}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Time</dt>
          <dd className="mt-0.5 font-mono text-[11px] text-foreground">
            {new Date(event.occurred_at).toLocaleString()}
          </dd>
        </div>
        {event.duration_ms !== null && (
          <div>
            <dt className="text-muted-foreground">Duration</dt>
            <dd className="mt-0.5 text-foreground">{formatDuration(event.duration_ms)}</dd>
          </div>
        )}
        <div>
          <dt className="text-muted-foreground">Attribution</dt>
          <dd className="mt-0.5 flex items-center gap-1.5 text-foreground">
            {event.attribution_confidence === 'unknown' && (
              <ShieldAlert className="h-3.5 w-3.5 text-amber-600 dark:text-amber-400" aria-hidden="true" />
            )}
            {event.attribution_confidence}
          </dd>
        </div>
      </dl>

      {resources.length > 0 && (
        <section className="mt-6">
          <h4 className="font-serif text-[11px] italic tracking-hearth-label text-muted-foreground">
            Resources
          </h4>
          <ul className="mt-2 space-y-1.5">
            {resources.map((resource) => (
              <li key={resource.id} className="flex items-start gap-2 rounded-md bg-muted/60 p-2 text-xs">
                <ResourceIcon kind={resource.kind} />
                <div className="min-w-0">
                  <p className="break-all font-mono text-[10px]">{resource.resource_ref}</p>
                  <p className="mt-0.5 text-[10px] capitalize text-muted-foreground">{resource.action}</p>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {Object.keys(event.metadata).length > 0 && (
        <section className="mt-6">
          <h4 className="font-serif text-[11px] italic tracking-hearth-label text-muted-foreground">
            Metadata
          </h4>
          <dl className="mt-2 space-y-2">
            {Object.entries(event.metadata).map(([key, value]) => (
              <div key={key} className="flex items-baseline justify-between gap-3 text-[11px]">
                <dt className="text-muted-foreground">{key.replaceAll('_', ' ')}</dt>
                <dd className="truncate font-mono text-foreground">{value}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}
    </div>
  );
}

function RunDebugger({
  detail,
  selectedEventId,
  onSelectEvent,
}: {
  detail: AgentRunDetail;
  selectedEventId: string | null;
  onSelectEvent: (event: AgentEvent) => void;
}) {
  const selectedEvent =
    detail.events.find((event) => event.id === selectedEventId) ?? detail.events.at(-1) ?? null;
  const resources = selectedEvent === null
    ? []
    : detail.resources.filter((resource) => resource.event_id === selectedEvent.id);

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="flex min-h-16 items-center justify-between gap-4 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="truncate text-sm font-semibold capitalize">
              {detail.run.provider} / {detail.run.project ?? 'Unknown project'}
            </h2>
            {detail.run.status === 'active' && (
              <span className="flex items-center gap-1 rounded-full bg-sage/10 px-2 py-0.5 text-[10px] font-medium text-sage-foreground">
                <Radio className="h-3 w-3" aria-hidden="true" /> Live
              </span>
            )}
            {detail.run.control_mode === 'managed' && (
              <span
                className="rounded-full border border-primary/20 bg-primary/5 px-2 py-0.5 text-[10px] font-medium text-primary"
                title={detail.run.capture_note ?? undefined}
              >
                Managed by Potluck
              </span>
            )}
            {detail.run.provider === 'codex' && detail.run.capture_profile === 'metadata-only' && (
              <span
                className="rounded-full border border-amber-500/25 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-700 dark:text-amber-300"
                title={detail.run.capture_note ?? undefined}
              >
                Partial capture
              </span>
            )}
          </div>
          <p className="mt-1 truncate font-mono text-[10px] text-muted-foreground">
            {detail.run.model ?? 'Model not reported'} · {detail.run.provider_session_id}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-4 text-[11px] text-muted-foreground">
          <span className="flex items-center gap-1">
            <Bot className="h-3.5 w-3.5" aria-hidden="true" /> {detail.run.agent_count}
          </span>
          <span className="flex items-center gap-1">
            <Clock3 className="h-3.5 w-3.5" aria-hidden="true" /> {runDuration(detail.run)}
          </span>
          <span>{detail.run.event_count} events</span>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1 overflow-auto">
          <Timeline
            detail={detail}
            selectedEventId={selectedEvent?.id ?? null}
            onSelectEvent={onSelectEvent}
          />
        </div>
        <aside className="flex w-72 shrink-0 flex-col border-l border-border bg-surface-1" aria-label="Event inspector">
          <div className="flex h-10 items-center border-b border-border/60 px-4 font-serif text-[11px] italic tracking-hearth-label text-muted-foreground">
            Inspector
          </div>
          <EventInspector event={selectedEvent} resources={resources} />
        </aside>
      </div>
    </div>
  );
}

export function AgentRunsPanel() {
  const navigate = useNavigate();
  const {
    runs,
    selectedRunId,
    detail,
    selectedEventId,
    isFetching,
    lastError,
    selectRun,
    selectEvent,
  } = useAgentRuns();

  useEffect(() => {
    if (selectedRunId === null && runs[0] !== undefined) void selectRun(runs[0].id);
  }, [runs, selectedRunId, selectRun]);

  const activeCount = useMemo(() => runs.filter((run) => run.status === 'active').length, [runs]);

  if (!isFetching && runs.length === 0) {
    return (
      <EmptyState
        icon={GitBranch}
        title="No execution runs yet"
        description="Connect Codex or Claude Code for this project, then start a new agent session. Potluck will show its observable activity here."
        primaryAction={(
          <button
            type="button"
            onClick={() => navigate('/settings?tab=connect')}
            className={cn(
              'inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground',
              'shadow-hearth-sm transition-colors hover:bg-primary/90',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            Connect an agent
          </button>
        )}
        secondaryAction={(
          <span className="text-[11px] text-muted-foreground">
            Prompts, commands, results, and file contents stay out of the trace.
          </span>
        )}
      />
    );
  }

  return (
    <div className="flex min-h-0 flex-1 overflow-hidden" data-testid="agent-runs-panel">
      <aside className="flex w-64 shrink-0 flex-col border-r border-border bg-surface-1" aria-label="Agent runs">
        <div className="flex h-10 items-center justify-between border-b border-border/60 px-3">
          <span className="font-serif text-[11px] italic tracking-hearth-label text-muted-foreground">Runs</span>
          <span className="text-[10px] text-muted-foreground">
            {activeCount > 0 ? `${activeCount} live` : `${runs.length} total`}
          </span>
        </div>
        <div className="flex-1 space-y-2 overflow-y-auto p-2">
          {runs.map((run) => (
            <RunCard
              key={run.id}
              run={run}
              selected={run.id === selectedRunId}
              onSelect={(id) => void selectRun(id)}
            />
          ))}
        </div>
      </aside>

      {detail !== null ? (
        <RunDebugger detail={detail} selectedEventId={selectedEventId} onSelectEvent={selectEvent} />
      ) : (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          {lastError ?? (isFetching ? 'Loading runs…' : 'Select a run')}
        </div>
      )}
    </div>
  );
}
