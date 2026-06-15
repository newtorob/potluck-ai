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
import { useProject } from '@/hooks/useProject';
import { TaskCard } from '@/components/tasks/TaskCard';
import { normaliseProjectName, resolveProjectFilter } from '@/lib/projectUtils';
import type { ProjectFilter } from '@/lib/projectUtils';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';
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
  onCreate: (name: string, objective: string, steps: string[], project: string | null) => Promise<void>;
  onCancel: () => void;
  defaultProject?: string | null;
  projects?: string[];
}

function NewTaskForm({ onCreate, onCancel, defaultProject, projects = [] }: NewTaskFormProps) {
  const [taskName, setTaskName] = useState('');
  const [objective, setObjective] = useState('');
  const [stepsText, setStepsText] = useState('');
  const [projectValue, setProjectValue] = useState<string>(defaultProject ?? '__global__');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  const handleProjectChange = (val: string) => {
    if (val === '__other__') {
      const raw = window.prompt('Project name:');
      if (!raw) return;
      const name = normaliseProjectName(raw);
      if (!name) return;
      setProjectValue(name);
      return;
    }
    setProjectValue(val);
  };

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

    const project: string | null = projectValue === '__global__' ? null : projectValue;

    setSubmitting(true);
    setError(null);
    try {
      await onCreate(trimName, trimObjective, steps, project);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create task');
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={(e) => { void handleSubmit(e); }}
      className="rounded-xl border border-border/60 bg-card p-4 shadow-hearth-sm space-y-3 animate-fade-in-up"
      aria-label="New task form"
    >
      <div className="flex items-center justify-between">
        <p className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
          New task
        </p>
        <button
          type="button"
          onClick={onCancel}
          aria-label="Cancel new task"
          className={cn(
            'flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground',
            'hover:bg-accent hover:text-foreground transition-colors duration-fast',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
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
            'placeholder:text-muted-foreground/70',
            'transition-all duration-base',
            'focus:outline-none focus:border-primary/40 focus:ring-2 focus:ring-ring/50 focus:shadow-hearth-sm',
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
            'w-full resize-none rounded-lg border border-input bg-background px-3 py-2 text-sm leading-relaxed',
            'placeholder:text-muted-foreground/70',
            'transition-all duration-base',
            'focus:outline-none focus:border-primary/40 focus:ring-2 focus:ring-ring/50 focus:shadow-hearth-sm',
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
            'w-full resize-none rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs leading-relaxed',
            'placeholder:text-muted-foreground/70',
            'transition-all duration-base',
            'focus:outline-none focus:border-primary/40 focus:ring-2 focus:ring-ring/50 focus:shadow-hearth-sm',
            'disabled:opacity-50',
          )}
        />
      </div>

      {/* Project field */}
      <div className="space-y-1">
        <label htmlFor="task-project" className="text-xs font-medium text-foreground">
          Project
        </label>
        <select
          id="task-project"
          value={projectValue}
          onChange={(e) => handleProjectChange(e.target.value)}
          disabled={submitting}
          className={cn(
            'w-full rounded-lg border border-input bg-background px-3 py-2 text-sm text-foreground',
            'transition-all duration-base',
            'focus:outline-none focus:border-primary/40 focus:ring-2 focus:ring-ring/50',
            'disabled:opacity-50',
          )}
        >
          <option value="__global__">Global (no project)</option>
          {defaultProject && (
            <option value={defaultProject}>{defaultProject} (active)</option>
          )}
          {projects
            .filter((p) => p !== defaultProject)
            .map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          <option value="__other__">+ Other…</option>
        </select>
      </div>

      <div className="flex gap-2 justify-end pt-1">
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className={cn(
            'rounded-lg border border-border px-3 py-1.5 text-xs font-medium',
            'text-muted-foreground hover:text-foreground hover:bg-accent hover:border-border',
            'transition-colors duration-fast',
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
            'rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground shadow-hearth-sm',
            'hover:bg-primary/90 active:scale-[0.98]',
            'transition-all duration-fast',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card',
            'disabled:opacity-50 disabled:active:scale-100',
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
  const [projectFilter, setProjectFilter] = useState<ProjectFilter>({ kind: 'active' });

  const { projects, activeProject } = useProject();

  // Resolve project params for the tasks fetch
  const { project: resolvedProject, includeAll } = resolveProjectFilter(projectFilter, activeProject);

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
  } = useTasks({
    ...(resolvedProject !== undefined ? { project: resolvedProject } : {}),
    ...(includeAll !== undefined ? { include_all: includeAll } : {}),
  });

  // Client-side filter on top of the full list so switching tabs is instant
  const filtered = tasks
    .filter((t) => activeFilter === 'all' || t.status === activeFilter)
    .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());

  const isTemporaryProjectFilter = (() => {
    if (projectFilter.kind === 'active') return false;
    if (projectFilter.kind === 'all') return true;
    if (projectFilter.kind === 'global') return activeProject !== null;
    if (projectFilter.kind === 'project') return projectFilter.name !== activeProject;
    return false;
  })();

  const handleCreate = async (name: string, objective: string, steps: string[], project: string | null) => {
    await createTask({
      task_name: name,
      objective,
      ...(steps.length > 0 ? { pending_steps: steps } : {}),
      project,
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
      <ViewHeader
        title={
          <span className="flex items-center gap-3">
            Tasks
            {isFetching && (
              <span
                className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-primary/30 border-t-primary"
                role="status"
                aria-label="Loading tasks"
              />
            )}
          </span>
        }
        count={tasks.length}
        actions={
          <button
            type="button"
            onClick={() => setShowNewForm(true)}
            disabled={showNewForm}
            className={cn(
              'flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground shadow-hearth-sm',
              'hover:bg-primary/90 active:scale-[0.98]',
              'transition-all duration-fast',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              'disabled:opacity-50 disabled:active:scale-100',
            )}
            aria-label="Create new task"
          >
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            New task
          </button>
        }
      />

      {/* Status filter chips */}
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
              'shrink-0 rounded-full px-3 py-1 text-xs font-medium tabular-nums',
              'transition-all duration-fast',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              activeFilter === value
                ? 'bg-primary text-primary-foreground shadow-hearth-sm'
                : 'bg-muted/70 text-muted-foreground hover:bg-accent hover:text-foreground',
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Project filter chips */}
      <div
        role="group"
        aria-label="Filter tasks by project"
        className="flex flex-wrap items-center gap-1.5 border-b border-border px-6 py-2.5"
      >
        {/* Active-project default — distinguished as the resting filter */}
        <button
          type="button"
          aria-pressed={projectFilter.kind === 'active'}
          onClick={() => setProjectFilter({ kind: 'active' })}
          className={cn(
            'shrink-0 rounded-full px-3 py-1 text-xs font-medium border',
            'transition-all duration-fast',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            projectFilter.kind === 'active'
              ? 'bg-primary/12 text-primary border-primary/25 shadow-hearth-sm'
              : 'bg-muted/70 text-muted-foreground border-transparent hover:bg-accent hover:text-foreground',
          )}
        >
          {activeProject ?? 'Global'}
          {projectFilter.kind === 'active' && (
            <span className="ml-1 italic opacity-60">(default)</span>
          )}
        </button>

        <button
          type="button"
          aria-pressed={projectFilter.kind === 'all'}
          onClick={() => setProjectFilter({ kind: 'all' })}
          className={cn(
            'shrink-0 rounded-full px-3 py-1 text-xs font-medium border border-transparent',
            'transition-all duration-fast',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            projectFilter.kind === 'all'
              ? 'bg-primary text-primary-foreground shadow-hearth-sm'
              : 'bg-muted/70 text-muted-foreground hover:bg-accent hover:text-foreground',
          )}
        >
          All (browse)
        </button>

        <button
          type="button"
          aria-pressed={projectFilter.kind === 'global'}
          onClick={() => setProjectFilter({ kind: 'global' })}
          className={cn(
            'shrink-0 rounded-full px-3 py-1 text-xs font-medium border border-transparent',
            'transition-all duration-fast',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            projectFilter.kind === 'global'
              ? 'bg-primary text-primary-foreground shadow-hearth-sm'
              : 'bg-muted/70 text-muted-foreground hover:bg-accent hover:text-foreground',
          )}
        >
          Global
        </button>

        {projects.map((p) => (
          <button
            key={p.project}
            type="button"
            aria-pressed={projectFilter.kind === 'project' && projectFilter.name === p.project}
            onClick={() => setProjectFilter({ kind: 'project', name: p.project })}
            className={cn(
              'shrink-0 rounded-full px-3 py-1 text-xs font-medium font-mono tracking-tight border border-transparent',
              'transition-all duration-fast',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              projectFilter.kind === 'project' && projectFilter.name === p.project
                ? 'bg-primary text-primary-foreground shadow-hearth-sm'
                : 'bg-muted/70 text-muted-foreground hover:bg-accent hover:text-foreground',
            )}
          >
            {p.project}
          </button>
        ))}

        {isTemporaryProjectFilter && (
          <span className="text-[10px] italic text-muted-foreground/60">(temporary)</span>
        )}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-6 py-4">
        <div className="mx-auto max-w-2xl space-y-3">
          {/* Inline new task form */}
          {showNewForm && (
            <NewTaskForm
              onCreate={handleCreate}
              onCancel={() => setShowNewForm(false)}
              defaultProject={activeProject}
              projects={projects.map((p) => p.project)}
            />
          )}

          {/* Error state */}
          {fetchError && (
            <div
              role="alert"
              className="animate-fade-in-up rounded-lg border border-destructive/25 bg-destructive/8 px-4 py-3 text-sm text-destructive shadow-hearth-sm"
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
            <EmptyState
              icon={ListTodo}
              title={activeFilter === 'all' ? 'No tasks yet' : `No ${activeFilter} tasks`}
              description={activeFilter === 'all'
                ? 'Tasks let an agent resume work across sessions. Break a goal into steps and check them off as you go.'
                : undefined}
              align="top"
            />
          )}
        </div>
      </div>
    </div>
  );
}
