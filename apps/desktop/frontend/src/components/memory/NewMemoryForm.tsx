/**
 * NewMemoryForm - inline form for creating a new memory.
 *
 * Rendered inline below the header when the "+ New" button is clicked.
 * Submitting or pressing Escape closes the form.
 */

import { useState, useRef, useEffect } from 'react';
import { cn } from '@/components/ui/cn';
import { useMemoryStore } from '@/stores/memoryStore';
import { normaliseProjectName } from '@/lib/projectUtils';
import type { MemoryCategory } from '@/types/memory';

const ALL_CATEGORIES: MemoryCategory[] = [
  'preference', 'fact', 'decision', 'project', 'person', 'instruction',
];

interface NewMemoryFormProps {
  onClose: () => void;
  defaultProject?: string | null;
  projects?: string[];
}

export function NewMemoryForm({ onClose, defaultProject, projects = [] }: NewMemoryFormProps) {
  const { createMemory } = useMemoryStore();
  const [content, setContent] = useState('');
  const [category, setCategory] = useState<MemoryCategory>('fact');
  const [confidence, setConfidence] = useState(0.8);
  // '__global__' is the sentinel for "no project"; actual project names are plain strings
  const [projectValue, setProjectValue] = useState<string>(
    defaultProject ?? '__global__',
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const resolvedProject: string | null = (() => {
    if (projectValue === '__global__') return null;
    if (projectValue === '__other__') return null; // handled via prompt
    return projectValue;
  })();

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

  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!content.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await createMemory({ content: content.trim(), category, confidence, project: resolvedProject });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create memory');
    } finally {
      setSaving(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') onClose();
  };

  return (
    <form
      onSubmit={(e) => void handleSubmit(e)}
      onKeyDown={handleKeyDown}
      className={cn(
        'rounded-xl border border-primary/40 bg-card px-4 py-4',
        'shadow-sm',
      )}
      aria-label="New memory form"
    >
      <h2 className="mb-3 text-sm font-semibold text-foreground">New memory</h2>

      <div className="space-y-3">
        {/* Content */}
        <div>
          <label
            htmlFor="new-memory-content"
            className="mb-1 block text-xs font-medium text-muted-foreground"
          >
            Content <span aria-hidden="true">*</span>
          </label>
          <textarea
            id="new-memory-content"
            ref={textareaRef}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={3}
            placeholder="Enter the memory content…"
            aria-required="true"
            className={cn(
              'w-full resize-y rounded-lg border border-input bg-background px-3 py-2',
              'text-sm text-foreground placeholder:text-muted-foreground',
              'focus:outline-none focus:ring-2 focus:ring-ring',
            )}
          />
        </div>

        {/* Category + Confidence row */}
        <div className="flex gap-3">
          <div className="flex-1">
            <label
              htmlFor="new-memory-category"
              className="mb-1 block text-xs font-medium text-muted-foreground"
            >
              Category
            </label>
            <select
              id="new-memory-category"
              value={category}
              onChange={(e) => setCategory(e.target.value as MemoryCategory)}
              className={cn(
                'w-full rounded-lg border border-input bg-background px-3 py-2',
                'text-sm text-foreground',
                'focus:outline-none focus:ring-2 focus:ring-ring',
              )}
            >
              {ALL_CATEGORIES.map((cat) => (
                <option key={cat} value={cat}>
                  {cat.charAt(0).toUpperCase() + cat.slice(1)}
                </option>
              ))}
            </select>
          </div>

          <div className="flex-1">
            <label
              htmlFor="new-memory-confidence"
              className="mb-1 flex items-center justify-between text-xs font-medium text-muted-foreground"
            >
              <span>Confidence</span>
              <span className="tabular-nums">{Math.round(confidence * 100)}%</span>
            </label>
            <input
              id="new-memory-confidence"
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={confidence}
              onChange={(e) => setConfidence(Number(e.target.value))}
              className="mt-2 w-full accent-primary"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(confidence * 100)}
            />
          </div>
        </div>

        {/* Project field */}
        <div>
          <label
            htmlFor="new-memory-project"
            className="mb-1 block text-xs font-medium text-muted-foreground"
          >
            Project
          </label>
          <select
            id="new-memory-project"
            value={projectValue}
            onChange={(e) => handleProjectChange(e.target.value)}
            className={cn(
              'w-full rounded-lg border border-input bg-background px-3 py-2',
              'text-sm text-foreground',
              'focus:outline-none focus:ring-2 focus:ring-ring',
            )}
          >
            <option value="__global__">Global (no project)</option>
            {defaultProject && defaultProject !== '__global__' && (
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

        {error && (
          <p className="text-xs text-destructive" role="alert">
            {error}
          </p>
        )}

        {/* Actions */}
        <div className="flex items-center gap-2 pt-1">
          <button
            type="submit"
            disabled={saving || !content.trim()}
            className={cn(
              'rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
              'hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            {saving ? 'Saving…' : 'Save memory'}
          </button>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className={cn(
              'rounded-lg px-3 py-1.5 text-xs font-medium text-muted-foreground',
              'hover:bg-muted hover:text-foreground transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            Cancel
          </button>
        </div>
      </div>
    </form>
  );
}
