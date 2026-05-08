/**
 * SessionDetailDrawer — slide-in panel from the right showing full session metadata
 * and chronological tool call timeline.
 *
 * - Width: 480px on desktop, pushes main content (flex layout)
 * - Focus trap: focus moves into the drawer on open; Esc closes
 * - Accessible: role="complementary", aria-label="Session detail"
 */

import { useEffect, useRef } from 'react';
import { X } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { ProjectPill } from '@/components/memory/ProjectPill';
import { relativeTime, fullDateTime } from '@/lib/timeFormat';
import type { SessionDetail, ToolCall } from '@/types/agents';

// ---------------------------------------------------------------------------
// Metadata row helper
// ---------------------------------------------------------------------------

interface MetaRowProps {
  label: string;
  value: React.ReactNode;
}

function MetaRow({ label, value }: MetaRowProps) {
  return (
    <div className="flex items-start justify-between gap-3 py-1.5 border-b border-border/50 last:border-0">
      <span className="shrink-0 text-xs font-medium text-muted-foreground w-28">{label}</span>
      <span className="min-w-0 text-right text-xs text-foreground break-all">{value}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tool call timeline row
// ---------------------------------------------------------------------------

interface TimelineRowProps {
  call: ToolCall;
}

function TimelineRow({ call }: TimelineRowProps) {
  return (
    <li className="flex flex-col gap-0.5 border-b border-border/30 py-2 last:border-0">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-xs font-medium text-foreground">{call.tool_name}</span>
        <span className="shrink-0 text-[10px] text-muted-foreground/70">
          {fullDateTime(call.called_at)}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        {call.result_count !== null && (
          <span className="text-sage">{call.result_count} results</span>
        )}
        {call.duration_ms !== null && call.error === null && (
          <span>{call.duration_ms}ms</span>
        )}
        {call.error !== null && (
          <span
            className="text-destructive truncate max-w-full"
            title={call.error}
          >
            Error: {call.error}
          </span>
        )}
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// SessionDetailDrawer
// ---------------------------------------------------------------------------

interface SessionDetailDrawerProps {
  detail: SessionDetail | null;
  onClose: () => void;
}

export function SessionDetailDrawer({ detail, onClose }: SessionDetailDrawerProps) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLDivElement>(null);

  // Move focus into the drawer on open
  useEffect(() => {
    if (detail !== null) {
      closeButtonRef.current?.focus();
    }
  }, [detail]);

  // Esc key closes
  useEffect(() => {
    if (detail === null) return;

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        onClose();
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [detail, onClose]);

  // Focus trap — keep Tab cycling within the drawer
  useEffect(() => {
    if (detail === null || drawerRef.current === null) return;

    const drawer = drawerRef.current;

    function handleTab(e: KeyboardEvent) {
      if (e.key !== 'Tab') return;
      const focusable = drawer.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      const focusableArray = Array.from(focusable);
      const first = focusableArray[0];
      const last = focusableArray[focusableArray.length - 1];

      if (e.shiftKey) {
        if (document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        }
      } else {
        if (document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    }

    drawer.addEventListener('keydown', handleTab);
    return () => drawer.removeEventListener('keydown', handleTab);
  }, [detail]);

  return (
    <div
      ref={drawerRef}
      role="complementary"
      aria-label="Session detail"
      aria-hidden={detail === null}
      className={cn(
        'flex h-full w-[480px] shrink-0 flex-col border-l border-border bg-card',
        'transition-[width] duration-200 overflow-hidden',
        detail === null ? 'w-0 border-0' : 'w-[480px]',
      )}
    >
      {detail !== null && (
        <>
          {/* Header */}
          <div className="flex items-center justify-between border-b border-border px-5 py-4">
            <h2 className="font-serif text-[11px] italic text-muted-foreground" style={{ letterSpacing: '0.04em' }}>Session detail</h2>
            <button
              ref={closeButtonRef}
              type="button"
              onClick={onClose}
              aria-label="Close session detail"
              className={cn(
                'flex h-7 w-7 items-center justify-center rounded-lg text-muted-foreground',
                'hover:bg-accent hover:text-foreground transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              )}
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>

          {/* Scrollable content */}
          <div className="flex-1 overflow-y-auto px-5 py-4 space-y-6">
            {/* Metadata block */}
            <section aria-label="Session metadata">
              <h3 className="mb-2 font-serif text-[11px] italic text-muted-foreground" style={{ letterSpacing: '0.04em' }}>
                Metadata
              </h3>
              <div className="rounded-lg border border-border bg-background/50 px-3 py-1">
                <MetaRow label="ID" value={<span className="font-mono text-[10px]">{detail.id}</span>} />
                <MetaRow label="Client" value={detail.client_name ?? '—'} />
                <MetaRow label="Version" value={detail.client_version ?? '—'} />
                <MetaRow label="Transport" value={detail.transport} />
                <MetaRow label="PID" value={detail.pid !== null ? String(detail.pid) : '—'} />
                <MetaRow label="Project" value={<ProjectPill project={detail.project} />} />
                <MetaRow
                  label="Model"
                  value={
                    detail.model !== null ? (
                      <span>
                        {detail.model}
                        {detail.model_source !== null && (
                          <span className="ml-1 text-muted-foreground">({detail.model_source})</span>
                        )}
                      </span>
                    ) : (
                      <span className="italic text-muted-foreground/60">unknown</span>
                    )
                  }
                />
                <MetaRow
                  label="Peer MCPs"
                  value={
                    detail.peer_mcps.length > 0
                      ? detail.peer_mcps.join(', ')
                      : <span className="italic text-muted-foreground/60">(none)</span>
                  }
                />
                <MetaRow label="Tool calls" value={String(detail.tool_call_count)} />
                <MetaRow label="Started" value={fullDateTime(detail.started_at)} />
                <MetaRow label="Last seen" value={relativeTime(detail.last_seen_at)} />
                {detail.disconnected_at !== null && (
                  <MetaRow label="Disconnected" value={fullDateTime(detail.disconnected_at)} />
                )}
              </div>
            </section>

            {/* Tool call timeline */}
            <section aria-label="Tool call timeline">
              <h3 className="mb-2 flex items-center gap-2 font-serif text-[11px] italic text-muted-foreground" style={{ letterSpacing: '0.04em' }}>
                Tool call timeline
                <span className="font-sans not-italic text-muted-foreground/60">
                  ({detail.recent_tool_calls.length} calls)
                </span>
              </h3>

              {detail.recent_tool_calls.length === 0 ? (
                <p className="text-xs text-muted-foreground/60 italic">No tool calls recorded for this session.</p>
              ) : (
                <ul className="rounded-lg border border-border bg-background/50 px-3">
                  {detail.recent_tool_calls
                    .slice()
                    .sort(
                      (a, b) =>
                        new Date(a.called_at).getTime() - new Date(b.called_at).getTime(),
                    )
                    .map((call) => (
                      <TimelineRow key={call.id} call={call} />
                    ))}
                </ul>
              )}
            </section>
          </div>
        </>
      )}
    </div>
  );
}
