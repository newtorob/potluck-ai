/**
 * TaskCard — renders a single TaskState with full inline editing.
 *
 * Sections:
 *   - Header: name, status pill, relative timestamp, actions menu
 *   - Objective (italic, muted)
 *   - Constraints (collapsible)
 *   - Steps: completed (strikethrough) + pending (clickable to complete) + add-step form
 *   - Files touched (chips, max 10 visible)
 *   - Errors (collapsible, add-error form)
 */

import { useState, useRef, useEffect } from 'react';
import {
  CheckCircle2,
  Circle,
  ChevronDown,
  ChevronUp,
  MoreHorizontal,
  Trash2,
  Play,
  Pause,
  Ban,
  CheckCheck,
  Plus,
  AlertTriangle,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import type { TaskState, TaskStatus } from '@/types/tasks';

// ---------------------------------------------------------------------------
// Status pill
// ---------------------------------------------------------------------------

const STATUS_STYLES: Record<TaskStatus, string> = {
  active: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
  paused: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
  completed: 'bg-blue-500/15 text-blue-600 dark:text-blue-400',
  abandoned: 'bg-muted text-muted-foreground',
};

function StatusPill({ status }: { status: TaskStatus }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium capitalize',
        STATUS_STYLES[status],
      )}
    >
      {status}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Relative time helper
// ---------------------------------------------------------------------------

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// ---------------------------------------------------------------------------
// Actions menu
// ---------------------------------------------------------------------------

interface ActionsMenuProps {
  status: TaskStatus;
  onSetStatus: (s: TaskStatus) => void;
  onDelete: () => void;
}

function ActionsMenu({ status, onSetStatus, onDelete }: ActionsMenuProps) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const actions: Array<{ label: string; icon: React.ReactNode; status?: TaskStatus; danger?: boolean }> = [
    ...(status !== 'active'
      ? [{ label: 'Resume', icon: <Play className="h-3.5 w-3.5" />, status: 'active' as const }]
      : []),
    ...(status === 'active'
      ? [{ label: 'Pause', icon: <Pause className="h-3.5 w-3.5" />, status: 'paused' as const }]
      : []),
    ...(status !== 'completed'
      ? [{ label: 'Complete', icon: <CheckCheck className="h-3.5 w-3.5" />, status: 'completed' as const }]
      : []),
    ...(status !== 'abandoned'
      ? [{ label: 'Abandon', icon: <Ban className="h-3.5 w-3.5" />, status: 'abandoned' as const }]
      : []),
  ];

  return (
    <div ref={menuRef} className="relative">
      <button
        type="button"
        aria-label="Task options"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground',
          'hover:bg-accent hover:text-foreground transition-colors',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        )}
      >
        <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
      </button>

      {open && (
        <div
          role="menu"
          className={cn(
            'absolute right-0 top-full z-50 mt-1 w-36 rounded-lg border border-border',
            'bg-popover py-1 shadow-lg',
          )}
        >
          {actions.map((a) => (
            <button
              key={a.label}
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                if (a.status !== undefined) onSetStatus(a.status);
              }}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-foreground hover:bg-accent transition-colors"
            >
              {a.icon}
              {a.label}
            </button>
          ))}
          <hr className="my-1 border-border" />
          <button
            type="button"
            role="menuitem"
            onClick={() => { setOpen(false); onDelete(); }}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-destructive hover:bg-destructive/10 transition-colors"
          >
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Steps section
// ---------------------------------------------------------------------------

const COMPLETED_VISIBLE_DEFAULT = 5;

interface StepsSectionProps {
  task: TaskState;
  onCompleteStep: (step: string) => void;
  onAddStep: (step: string) => void;
}

function StepsSection({ task, onCompleteStep, onAddStep }: StepsSectionProps) {
  const [showAllCompleted, setShowAllCompleted] = useState(false);
  const [addingStep, setAddingStep] = useState(false);
  const [newStep, setNewStep] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  const completed = task.completed_steps;
  const pending = task.pending_steps;
  const visibleCompleted = showAllCompleted
    ? completed
    : completed.slice(-COMPLETED_VISIBLE_DEFAULT);
  const hiddenCount = completed.length - COMPLETED_VISIBLE_DEFAULT;

  useEffect(() => {
    if (addingStep) inputRef.current?.focus();
  }, [addingStep]);

  const submitStep = () => {
    const trimmed = newStep.trim();
    if (!trimmed) {
      setAddingStep(false);
      return;
    }
    onAddStep(trimmed);
    setNewStep('');
    setAddingStep(false);
  };

  return (
    <div className="space-y-1">
      <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Steps</p>

      {/* Completed steps */}
      {completed.length > 0 && (
        <ul className="space-y-0.5" aria-label="Completed steps">
          {!showAllCompleted && hiddenCount > 0 && (
            <li>
              <button
                type="button"
                onClick={() => setShowAllCompleted(true)}
                className="text-xs text-muted-foreground hover:text-foreground underline underline-offset-2"
              >
                Show {hiddenCount} earlier {hiddenCount === 1 ? 'step' : 'steps'}
              </button>
            </li>
          )}
          {visibleCompleted.map((step, i) => (
            <li key={i} className="flex items-start gap-2 py-0.5">
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
              <span className="text-xs text-muted-foreground line-through">{step}</span>
            </li>
          ))}
        </ul>
      )}

      {/* Pending steps */}
      {pending.length > 0 && (
        <ul className="space-y-0.5" aria-label="Pending steps">
          {pending.map((step, i) => (
            <li key={i}>
              <button
                type="button"
                onClick={() => onCompleteStep(step)}
                aria-label={`Mark step complete: ${step}`}
                className={cn(
                  'flex w-full items-start gap-2 rounded-md px-1 py-0.5 text-left',
                  'text-xs text-foreground',
                  'hover:bg-accent hover:text-accent-foreground transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                )}
              >
                <Circle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                {step}
              </button>
            </li>
          ))}
        </ul>
      )}

      {completed.length === 0 && pending.length === 0 && (
        <p className="text-xs text-muted-foreground italic">No steps yet</p>
      )}

      {/* Add step form */}
      {addingStep ? (
        <div className="flex items-center gap-1 pl-5">
          <input
            ref={inputRef}
            value={newStep}
            onChange={(e) => setNewStep(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitStep();
              if (e.key === 'Escape') { setAddingStep(false); setNewStep(''); }
            }}
            onBlur={submitStep}
            placeholder="Describe the step…"
            className={cn(
              'flex-1 rounded border border-input bg-background px-2 py-1 text-xs',
              'focus:outline-none focus:ring-1 focus:ring-ring',
            )}
            aria-label="New step description"
          />
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setAddingStep(true)}
          className={cn(
            'flex items-center gap-1 pl-5 text-xs text-muted-foreground',
            'hover:text-foreground transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded',
          )}
        >
          <Plus className="h-3 w-3" aria-hidden="true" />
          Add step
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Errors section
// ---------------------------------------------------------------------------

interface ErrorsSectionProps {
  task: TaskState;
  onAddError: (error: string, resolution?: string) => void;
}

function ErrorsSection({ task, onAddError }: ErrorsSectionProps) {
  const [expanded, setExpanded] = useState(false);
  const [addingError, setAddingError] = useState(false);
  const [errorText, setErrorText] = useState('');
  const [resolutionText, setResolutionText] = useState('');

  const errors = task.errors_encountered;
  const visible = errors.slice(-3);

  const submitError = () => {
    const trimmed = errorText.trim();
    if (!trimmed) { setAddingError(false); return; }
    const res = resolutionText.trim();
    onAddError(trimmed, res !== '' ? res : undefined);
    setErrorText('');
    setResolutionText('');
    setAddingError(false);
  };

  if (errors.length === 0 && !addingError) {
    return (
      <button
        type="button"
        onClick={() => setAddingError(true)}
        className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <Plus className="h-3 w-3" aria-hidden="true" />
        Log error
      </button>
    );
  }

  return (
    <div className="space-y-1">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground"
        aria-expanded={expanded}
      >
        <AlertTriangle className="h-3 w-3 text-amber-500" aria-hidden="true" />
        Errors ({errors.length})
        {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
      </button>

      {expanded && (
        <div className="space-y-2 pl-1">
          {visible.map((e, i) => (
            <div key={i} className="rounded-md border border-border bg-muted/30 px-2 py-1.5 text-xs">
              <p className="font-medium text-destructive">{e.error}</p>
              {e.resolution !== '' && (
                <p className="mt-0.5 text-muted-foreground">Resolution: {e.resolution}</p>
              )}
              <p className="mt-0.5 text-[10px] text-muted-foreground/70">
                {relativeTime(e.timestamp)}
              </p>
            </div>
          ))}

          {addingError ? (
            <div className="space-y-1">
              <input
                value={errorText}
                onChange={(e) => setErrorText(e.target.value)}
                placeholder="Error description…"
                className={cn(
                  'w-full rounded border border-input bg-background px-2 py-1 text-xs',
                  'focus:outline-none focus:ring-1 focus:ring-ring',
                )}
                aria-label="Error description"
              />
              <input
                value={resolutionText}
                onChange={(e) => setResolutionText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') submitError();
                  if (e.key === 'Escape') setAddingError(false);
                }}
                placeholder="Resolution (optional)…"
                className={cn(
                  'w-full rounded border border-input bg-background px-2 py-1 text-xs',
                  'focus:outline-none focus:ring-1 focus:ring-ring',
                )}
                aria-label="Error resolution"
              />
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={submitError}
                  className="rounded bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90"
                >
                  Save
                </button>
                <button
                  type="button"
                  onClick={() => setAddingError(false)}
                  className="rounded px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setAddingError(true)}
              className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            >
              <Plus className="h-3 w-3" aria-hidden="true" />
              Add error
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main TaskCard
// ---------------------------------------------------------------------------

interface TaskCardProps {
  task: TaskState;
  onCompleteStep: (id: string, step: string) => void;
  onAddStep: (id: string, step: string) => void;
  onAddError: (id: string, error: string, resolution?: string) => void;
  onSetStatus: (id: string, status: TaskStatus) => void;
  onDelete: (id: string) => void;
}

export function TaskCard({
  task,
  onCompleteStep,
  onAddStep,
  onAddError,
  onSetStatus,
  onDelete,
}: TaskCardProps) {
  const [showConstraints, setShowConstraints] = useState(false);

  const FILES_VISIBLE = 10;
  const visibleFiles = task.files_touched.slice(0, FILES_VISIBLE);
  const hiddenFiles = task.files_touched.length - FILES_VISIBLE;

  return (
    <article
      className={cn(
        'rounded-xl border border-border bg-card p-4 shadow-sm',
        'flex flex-col gap-3',
      )}
      aria-label={`Task: ${task.task_name}`}
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2 min-w-0">
          <h2 className="text-sm font-semibold text-foreground truncate">{task.task_name}</h2>
          <StatusPill status={task.status} />
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className="text-[11px] text-muted-foreground whitespace-nowrap">
            {relativeTime(task.updated_at)}
          </span>
          <ActionsMenu
            status={task.status}
            onSetStatus={(s) => onSetStatus(task.id, s)}
            onDelete={() => onDelete(task.id)}
          />
        </div>
      </div>

      {/* Objective */}
      <p className="text-xs italic text-muted-foreground leading-relaxed">{task.objective}</p>

      {/* Constraints */}
      {task.known_constraints.length > 0 && (
        <div>
          <button
            type="button"
            onClick={() => setShowConstraints((v) => !v)}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
            aria-expanded={showConstraints}
          >
            {showConstraints ? (
              <ChevronUp className="h-3 w-3" aria-hidden="true" />
            ) : (
              <ChevronDown className="h-3 w-3" aria-hidden="true" />
            )}
            {showConstraints ? 'Hide' : 'Show'} constraints ({task.known_constraints.length})
          </button>
          {showConstraints && (
            <ul className="mt-1.5 space-y-0.5 pl-2" aria-label="Constraints">
              {task.known_constraints.map((c, i) => (
                <li key={i} className="flex items-start gap-1.5 text-xs text-muted-foreground">
                  <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-muted-foreground" aria-hidden="true" />
                  {c}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Steps */}
      <StepsSection
        task={task}
        onCompleteStep={(step) => onCompleteStep(task.id, step)}
        onAddStep={(step) => onAddStep(task.id, step)}
      />

      {/* Files touched */}
      {task.files_touched.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {visibleFiles.map((f, i) => (
            <span
              key={i}
              className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground truncate max-w-[180px]"
              title={f}
            >
              {f}
            </span>
          ))}
          {hiddenFiles > 0 && (
            <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              +{hiddenFiles} more
            </span>
          )}
        </div>
      )}

      {/* Errors */}
      <ErrorsSection
        task={task}
        onAddError={(error, resolution) => onAddError(task.id, error, resolution)}
      />
    </article>
  );
}
