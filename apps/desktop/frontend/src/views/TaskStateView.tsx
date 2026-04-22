/**
 * TaskStateView — /tasks route.
 *
 * Lists all agent task states with status filtering.
 * Inline form to create new tasks.
 * Each task rendered as a TaskCard with full editing.
 */

import { useState, useRef, useEffect } from 'react';
import { ListTodo, Plus, X } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { useTasks } from '@/hooks/useTasks';
import { TaskCard } from '@/components/tasks/TaskCard';
import type { TaskStatus } from '@/types/tasks';

// ---------------------------------------------------------------------------
// Filter chips
// ---------------------------------------------------------------------------

type FilterValue = 'all' | TaskStatus;

const FILTERS: Array<{ value: FilterValue; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'paused', label: 'Paused' },
  { value: 'completed', label: 'Completed' },
  { value: 'abandoned', label: 'Abandoned' },
];

// ---------------------------------------------------------------------------
// New task form
// ---------------------------------------------------------------------------

interface NewTaskFormProps {
  onCreate: (name: string, objective: string, steps: string[]) => Promise<void>;
  onCancel: () => void;
}

function NewTaskForm({ onCreate, onCancel }: NewTaskFormProps) {
  const [taskName, setTaskName] = useState('');
  const [objective, setObjective] = useState('');
  const [stepsText, setStepsText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimName = taskName.trim();
    const trimObjective = objective.trim();
    if (!trimName || !trimObjective) {
      setError('Name and objective are required.');
      return;
    }
    const steps = stepsText
      .split('\n')
      .map((s) => s.trim())
      .filter((s) => s.length > 0);

    setSubmitting(true);
    setError(null);
    try {
      await onCreate(trimName, trimObjective, steps);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create task');
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={(e) => { void handleSubmit(e); }}
      className="rounded-xl border border-border bg-card p-4 shadow-sm space-y-3"
      aria-label="New task form"
    >
      <div className="flex items-center justify-between">
        <p className="text-sm font-semibold text-foreground">New task</p>
        <button
          type="button"
          onClick={onCancel}
          aria-label="Cancel new task"
          className="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <X className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>

      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="space-y-1">
        <label htmlFor="task-name" className="text-xs font-medium text-foreground">
          Task name <span aria-hidden="true" className="text-destructive">*</span>
        </label>
        <input
          id="task-name"
          ref={nameRef}
          value={taskName}
          onChange={(e) => setTaskName(e.target.value)}
          placeholder="e.g. Refactor auth module"
          disabled={submitting}
          required
          className={cn(
            'w-full rounded-lg border border-input bg-background px-3 py-2 text-sm',
            'placeholder:text-muted-foreground',
            'focus:outline-none focus:ring-2 focus:ring-ring',
            'disabled:opacity-50',
          )}
        />
      </div>

      <div className="space-y-1">
        <label htmlFor="task-objective" className="text-xs font-medium text-foreground">
          Objective <span aria-hidden="true" className="text-destructive">*</span>
        </label>
        <textarea
          id="task-objective"
          value={objective}
          onChange={(e) => setObjective(e.target.value)}
          placeholder="Describe what this task accomplishes…"
          rows={2}
          disabled={submitting}
          required
          className={cn(
            'w-full resize-none rounded-lg border border-input bg-background px-3 py-2 text-sm',
            'placeholder:text-muted-foreground',
            'focus:outline-none focus:ring-2 focus:ring-ring',
            'disabled:opacity-50',
          )}
        />
      </div>

      <div className="space-y-1">
        <label htmlFor="task-steps" className="text-xs font-medium text-foreground">
          Pending steps
          <span className="ml-1 text-muted-foreground font-normal">(one per line, optional)</span>
        </label>
        <textarea
          id="task-steps"
          value={stepsText}
          onChange={(e) => setStepsText(e.target.value)}
          placeholder={`Read existing code\nWrite tests\nImplement feature`}
          rows={3}
          disabled={submitting}
          className={cn(
            'w-full resize-none rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs',
            'placeholder:text-muted-foreground',
            'focus:outline-none focus:ring-2 focus:ring-ring',
            'disabled:opacity-50',
          )}
        />
      </div>

      <div className="flex gap-2 justify-end">
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className={cn(
            'rounded-lg border border-border px-3 py-1.5 text-xs font-medium',
            'text-muted-foreground hover:text-foreground hover:bg-accent transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            'disabled:opacity-50',
          )}
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={submitting}
          className={cn(
            'rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
            'hover:bg-primary/90 transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            'disabled:opacity-50',
          )}
        >
          {submitting ? 'Creating…' : 'Create task'}
        </button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

export function TaskStateView() {
  const [activeFilter, setActiveFilter] = useState<FilterValue>('active');
  const [showNewForm, setShowNewForm] = useState(false);

  const {
    tasks,
    isFetching,
    fetchError,
    createTask,
    completeStep,
    updateTask,
    addError,
    deleteTask,
    setStatus,
  } = useTasks();

  // Client-side filter on top of the full list so switching tabs is instant
  const filtered = tasks
    .filter((t) => activeFilter === 'all' || t.status === activeFilter)
    .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());

  const handleCreate = async (name: string, objective: string, steps: string[]) => {
    await createTask({
      task_name: name,
      objective,
      pending_steps: steps.length > 0 ? steps : undefined,
    });
    setShowNewForm(false);
  };

  const handleAddStep = (id: string, step: string) => {
    const task = tasks.find((t) => t.id === id);
    if (!task) return;
    void updateTask(id, { pending_steps: [...task.pending_steps, step] });
  };

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-6 py-4">
        <div className="flex items-center gap-3">
          <ListTodo className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
          <h1 className="text-sm font-semibold">Tasks</h1>
          {isFetching && (
            <div
              className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-primary border-t-transparent"
              role="status"
              aria-label="Loading tasks"
            />
          )}
        </div>
        <button
          type="button"
          onClick={() => setShowNewForm(true)}
          disabled={showNewForm}
          className={cn(
            'flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
            'hover:bg-primary/90 transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            'disabled:opacity-50',
          )}
          aria-label="Create new task"
        >
          <Plus className="h-3.5 w-3.5" aria-hidden="true" />
          New task
        </button>
      </div>

      {/* Filter chips */}
      <div
        role="group"
        aria-label="Filter tasks by status"
        className="flex gap-1.5 overflow-x-auto border-b border-border px-6 py-2.5"
      >
        {FILTERS.map(({ value, label }) => (
          <button
            key={value}
            type="button"
            aria-pressed={activeFilter === value}
            onClick={() => setActiveFilter(value)}
            className={cn(
              'shrink-0 rounded-full px-3 py-1 text-xs font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              activeFilter === value
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:bg-accent hover:text-foreground',
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-6 py-4">
        <div className="mx-auto max-w-2xl space-y-3">
          {/* Inline new task form */}
          {showNewForm && (
            <NewTaskForm
              onCreate={handleCreate}
              onCancel={() => setShowNewForm(false)}
            />
          )}

          {/* Error state */}
          {fetchError && (
            <div
              role="alert"
              className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
            >
              {fetchError}
            </div>
          )}

          {/* Task list */}
          {filtered.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              onCompleteStep={completeStep}
              onAddStep={handleAddStep}
              onAddError={(id, error, resolution) => { void addError(id, error, resolution); }}
              onSetStatus={(id, status) => { void setStatus(id, status); }}
              onDelete={(id) => { void deleteTask(id); }}
            />
          ))}

          {/* Empty state */}
          {!fetchError && filtered.length === 0 && !showNewForm && (
            <div className="flex flex-col items-center gap-3 py-16 text-center">
              <ListTodo className="h-10 w-10 text-muted-foreground/40" aria-hidden="true" />
              <div className="space-y-1">
                <p className="text-sm font-medium text-muted-foreground">
                  {activeFilter === 'all' ? 'No tasks yet' : `No ${activeFilter} tasks`}
                </p>
                {activeFilter === 'all' && (
                  <p className="text-xs text-muted-foreground/70">
                    Tasks let an agent resume work across sessions.
                  </p>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
