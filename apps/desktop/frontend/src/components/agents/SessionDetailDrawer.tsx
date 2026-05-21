/**
 * SessionDetailDrawer — slide-in panel from the right showing full session metadata
 * and chronological tool call timeline.
 *
 * - Width: 480px on desktop, pushes main content (flex layout)
 * - Focus trap: focus moves into the drawer on open; Esc closes
 * - Accessible: role="complementary", aria-label="Session detail"
 */

import { useEffect, useRef, useState } from 'react';
import { ChevronRight, Download, X } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { ProjectPill } from '@/components/memory/ProjectPill';
import { relativeTime, fullDateTime } from '@/lib/timeFormat';
import { localApi } from '@/lib/localApi';
import type { MemoryPreview, SessionDetail, ToolCall } from '@/types/agents';

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
  const [expanded, setExpanded] = useState(false);
  const [previews, setPreviews] = useState<MemoryPreview[] | null>(null);
  const [previewsLoading, setPreviewsLoading] = useState(false);
  const [previewsError, setPreviewsError] = useState<string | null>(null);
  const hasError = call.error !== null;
  const hasArgs = call.args_summary !== null && Object.keys(call.args_summary).length > 0;
  const hasVersion = call.tool_schema_version !== null;
  const memoryIds = call.memory_ids_touched ?? [];

  // Lazy-load memory previews the first time the row is expanded AND it
  // touched memories. No prefetch — the drawer can show 200 rows; eager
  // loading every preview would be wasteful.
  useEffect(() => {
    if (!expanded || memoryIds.length === 0 || previews !== null || previewsLoading) return;
    setPreviewsLoading(true);
    setPreviewsError(null);
    localApi.agents
      .memoryPreviews(memoryIds)
      .then((rows) => setPreviews(rows))
      .catch((err: unknown) => {
        setPreviewsError(err instanceof Error ? err.message : 'Could not load memory previews');
      })
      .finally(() => setPreviewsLoading(false));
  }, [expanded, memoryIds, previews, previewsLoading]);

  return (
    <li
      className={cn(
        'border-b border-border/30 last:border-0',
        // Red left-border + tint when the call errored — the "5-second screenshot"
        // moment: the debugger reader can see at a glance which row went wrong.
        hasError && 'border-l-2 border-l-destructive bg-destructive/5',
      )}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        aria-label={expanded ? `Collapse ${call.tool_name} details` : `Expand ${call.tool_name} details`}
        className={cn(
          'flex w-full flex-col gap-0.5 px-2 py-2 text-left',
          'hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm',
        )}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-1.5 font-mono text-xs font-medium text-foreground">
            <ChevronRight
              className={cn(
                'h-3 w-3 text-muted-foreground transition-transform',
                expanded && 'rotate-90',
              )}
              aria-hidden="true"
            />
            {call.tool_name}
          </span>
          <span className="shrink-0 text-[10px] text-muted-foreground/70">
            {fullDateTime(call.called_at)}
          </span>
        </div>

        <div className="ml-[18px] flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          {call.result_count !== null && (
            <span className="text-sage">{call.result_count} results</span>
          )}
          {call.duration_ms !== null && !hasError && (
            <span>{call.duration_ms}ms</span>
          )}
          {hasError && (
            <span className="text-destructive font-medium">errored</span>
          )}
          {hasVersion && (
            <span
              className="rounded-sm bg-muted/40 px-1.5 py-px font-mono text-[10px] text-muted-foreground/80"
              title="MCP tool-schema contract version this call ran under"
            >
              {call.tool_schema_version}
            </span>
          )}
        </div>
      </button>

      {expanded && (
        <div className="ml-[18px] mb-2 mr-2 space-y-2 rounded-sm border border-border/40 bg-background/70 px-2.5 py-2 text-[11px]">
          {hasArgs ? (
            <div>
              <div className="mb-1 font-medium text-muted-foreground">Args (metadata only)</div>
              <pre className="overflow-x-auto whitespace-pre-wrap break-all font-mono text-[10.5px] text-foreground/90">
                {JSON.stringify(call.args_summary, null, 2)}
              </pre>
            </div>
          ) : (
            <div className="italic text-muted-foreground/70">
              No args recorded — either none passed or all stripped by the privacy whitelist.
            </div>
          )}

          {hasError && (
            <div>
              <div className="mb-1 font-medium text-destructive">Error</div>
              <pre className="overflow-x-auto whitespace-pre-wrap break-all font-mono text-[10.5px] text-destructive/90">
                {call.error}
              </pre>
            </div>
          )}

          {memoryIds.length > 0 && (
            <div>
              <div className="mb-1 flex items-center gap-1.5 font-medium text-muted-foreground">
                Memories touched
                <span className="font-sans text-muted-foreground/60">({memoryIds.length})</span>
              </div>
              {previewsLoading && (
                <div className="italic text-muted-foreground/60">Loading previews…</div>
              )}
              {previewsError !== null && (
                <div className="italic text-destructive/80">{previewsError}</div>
              )}
              {previews !== null && previews.length === 0 && (
                <div className="italic text-muted-foreground/60">
                  All referenced memories were deleted after this call.
                </div>
              )}
              {previews !== null && previews.length > 0 && (
                <ul className="space-y-1.5">
                  {previews.map((m) => (
                    <li
                      key={m.id}
                      className={cn(
                        'rounded-sm border border-border/40 bg-background/60 px-2 py-1.5',
                        !m.is_active && 'opacity-60',
                      )}
                    >
                      <div className="mb-0.5 flex items-center justify-between gap-2 text-[10px] uppercase tracking-wide text-muted-foreground/80">
                        <span>{m.category}</span>
                        {!m.is_active && <span className="italic">deleted</span>}
                      </div>
                      <p className="font-sans text-[11px] leading-snug text-foreground/90">
                        {m.preview}
                      </p>
                    </li>
                  ))}
                  {previews.length < memoryIds.length && (
                    <li className="italic text-muted-foreground/60">
                      {memoryIds.length - previews.length} additional{' '}
                      {memoryIds.length - previews.length === 1 ? 'memory was' : 'memories were'}{' '}
                      deleted since this call.
                    </li>
                  )}
                </ul>
              )}
            </div>
          )}

          <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 border-t border-border/40 pt-1.5 text-muted-foreground/80">
            <div>
              <span className="text-muted-foreground/60">id </span>
              <span className="font-mono text-[10px]">{call.id}</span>
            </div>
            <div>
              <span className="text-muted-foreground/60">schema </span>
              <span className="font-mono text-[10px]">
                {call.tool_schema_version ?? <em className="italic">pre-versioning</em>}
              </span>
            </div>
          </div>
        </div>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Export trace button — downloads a portable JSON for bug reports
// ---------------------------------------------------------------------------

function ExportTraceButton({ sessionId }: { sessionId: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleExport() {
    setBusy(true);
    setError(null);
    try {
      const { blob, filename } = await localApi.agents.exportSessionTrace(sessionId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Export failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      type="button"
      onClick={() => void handleExport()}
      disabled={busy}
      title={
        error !== null
          ? `Export failed: ${error}`
          : 'Download a portable JSON trace of this session — safe to share with bug reports.'
      }
      aria-label="Export session trace"
      className={cn(
        'flex h-7 w-7 items-center justify-center rounded-lg text-muted-foreground',
        'hover:bg-accent hover:text-foreground transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        'disabled:opacity-50 disabled:cursor-not-allowed',
        error !== null && 'text-destructive hover:text-destructive',
      )}
    >
      <Download className="h-4 w-4" aria-hidden="true" />
    </button>
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
            <h2 className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">Session detail</h2>
            <div className="flex items-center gap-1">
              <ExportTraceButton sessionId={detail.id} />
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
          </div>

          {/* Scrollable content */}
          <div className="flex-1 overflow-y-auto px-5 py-4 space-y-6">
            {/* Metadata block */}
            <section aria-label="Session metadata">
              <h3 className="mb-2 font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
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
              <h3 className="mb-2 flex items-center gap-2 font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
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
