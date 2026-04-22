/**
 * MemoryCard — displays a single memory with inline edit capability.
 *
 * Hover actions (top-right): Edit, Boost confidence, Demote confidence, Delete.
 * Edit mode renders a form with textarea + category dropdown + confidence slider.
 * Escape key cancels edit. Tab order is logical.
 */

import { useState, useRef, useEffect, useCallback } from 'react';
import { Pencil, Trash2, ChevronUp, ChevronDown } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { cn } from '@/components/ui/cn';
import { CategoryPill } from './CategoryPill';
import { ConfidenceBar } from './ConfidenceBar';
import { useMemoryStore } from '@/stores/memoryStore';
import type { Memory, MemoryCategory } from '@/types/memory';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const ALL_CATEGORIES: MemoryCategory[] = [
  'preference', 'fact', 'decision', 'project', 'person', 'instruction',
];

function formatRelativeTime(isoString: string): string {
  const diff = Date.now() - new Date(isoString).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 5) return `${weeks}w ago`;
  return new Date(isoString).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// ---------------------------------------------------------------------------
// MemoryCard
// ---------------------------------------------------------------------------

interface MemoryCardProps {
  memory: Memory;
}

export function MemoryCard({ memory }: MemoryCardProps) {
  const { updateMemory, deleteMemory, boostConfidence } = useMemoryStore();

  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState(memory.content);
  const [editCategory, setEditCategory] = useState<MemoryCategory>(memory.category);
  const [editConfidence, setEditConfidence] = useState(memory.confidence);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Focus textarea when entering edit mode
  useEffect(() => {
    if (editing) {
      requestAnimationFrame(() => {
        textareaRef.current?.focus();
        textareaRef.current?.select();
      });
    }
  }, [editing]);

  const handleEditOpen = useCallback(() => {
    setEditContent(memory.content);
    setEditCategory(memory.category);
    setEditConfidence(memory.confidence);
    setSaveError(null);
    setEditing(true);
  }, [memory.content, memory.category, memory.confidence]);

  const handleEditCancel = useCallback(() => {
    setEditing(false);
    setSaveError(null);
  }, []);

  const handleEditSave = useCallback(async () => {
    if (!editContent.trim()) return;
    setSaving(true);
    setSaveError(null);
    try {
      await updateMemory(memory.id, {
        content: editContent.trim(),
        category: editCategory,
        confidence: editConfidence,
      });
      setEditing(false);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  }, [memory.id, editContent, editCategory, editConfidence, updateMemory]);

  const handleDelete = useCallback(() => {
    if (window.confirm('Delete this memory?')) {
      void deleteMemory(memory.id);
    }
  }, [memory.id, deleteMemory]);

  const handleBoost = useCallback(() => {
    if (memory.confidence < 1) {
      void boostConfidence(memory.id, 0.1);
    }
  }, [memory.id, memory.confidence, boostConfidence]);

  const handleDemote = useCallback(() => {
    if (memory.confidence > 0) {
      void boostConfidence(memory.id, -0.1);
    }
  }, [memory.id, memory.confidence, boostConfidence]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') handleEditCancel();
    },
    [handleEditCancel],
  );

  return (
    <article
      className={cn(
        'group relative rounded-xl border border-border bg-card px-4 py-3',
        'transition-shadow hover:shadow-sm',
      )}
      aria-label={`Memory: ${memory.content.slice(0, 60)}`}
    >
      {/* Hover action buttons — top right */}
      {!editing && (
        <div
          className={cn(
            'absolute right-2 top-2 flex items-center gap-0.5',
            'opacity-0 transition-opacity group-hover:opacity-100',
          )}
          aria-label="Memory actions"
        >
          <ActionButton
            onClick={handleEditOpen}
            aria-label="Edit memory"
            title="Edit"
          >
            <Pencil className="h-3 w-3" aria-hidden="true" />
          </ActionButton>

          <ActionButton
            onClick={handleBoost}
            aria-label="Increase confidence"
            title="Boost confidence"
            disabled={memory.confidence >= 1}
          >
            <ChevronUp className="h-3 w-3" aria-hidden="true" />
          </ActionButton>

          <ActionButton
            onClick={handleDemote}
            aria-label="Decrease confidence"
            title="Demote confidence"
            disabled={memory.confidence <= 0}
          >
            <ChevronDown className="h-3 w-3" aria-hidden="true" />
          </ActionButton>

          <ActionButton
            onClick={handleDelete}
            aria-label="Delete memory"
            title="Delete"
            variant="destructive"
          >
            <Trash2 className="h-3 w-3" aria-hidden="true" />
          </ActionButton>
        </div>
      )}

      {editing ? (
        // ---- Edit mode -------------------------------------------------------
        <form
          onSubmit={(e) => { e.preventDefault(); void handleEditSave(); }}
          onKeyDown={handleKeyDown}
          aria-label="Edit memory form"
        >
          <div className="space-y-3">
            {/* Content textarea */}
            <div>
              <label
                htmlFor={`memory-content-${memory.id}`}
                className="mb-1 block text-xs font-medium text-muted-foreground"
              >
                Content
              </label>
              <textarea
                id={`memory-content-${memory.id}`}
                ref={textareaRef}
                value={editContent}
                onChange={(e) => setEditContent(e.target.value)}
                rows={3}
                className={cn(
                  'w-full resize-y rounded-lg border border-input bg-background px-3 py-2',
                  'text-sm text-foreground placeholder:text-muted-foreground',
                  'focus:outline-none focus:ring-2 focus:ring-ring',
                )}
                placeholder="Memory content…"
                aria-required="true"
              />
            </div>

            {/* Category select */}
            <div>
              <label
                htmlFor={`memory-category-${memory.id}`}
                className="mb-1 block text-xs font-medium text-muted-foreground"
              >
                Category
              </label>
              <select
                id={`memory-category-${memory.id}`}
                value={editCategory}
                onChange={(e) => setEditCategory(e.target.value as MemoryCategory)}
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

            {/* Confidence slider */}
            <div>
              <label
                htmlFor={`memory-confidence-${memory.id}`}
                className="mb-1 flex items-center justify-between text-xs font-medium text-muted-foreground"
              >
                <span>Confidence</span>
                <span className="tabular-nums">{Math.round(editConfidence * 100)}%</span>
              </label>
              <input
                id={`memory-confidence-${memory.id}`}
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={editConfidence}
                onChange={(e) => setEditConfidence(Number(e.target.value))}
                className="w-full accent-primary"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(editConfidence * 100)}
              />
            </div>

            {saveError && (
              <p className="text-xs text-destructive" role="alert">
                {saveError}
              </p>
            )}

            {/* Save / Cancel */}
            <div className="flex items-center gap-2">
              <button
                type="submit"
                disabled={saving || !editContent.trim()}
                className={cn(
                  'rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
                  'hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  'transition-colors',
                )}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                onClick={handleEditCancel}
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
      ) : (
        // ---- View mode -------------------------------------------------------
        <>
          {/* Top row: category pill */}
          <div className="mb-2 flex items-center gap-2 pr-20">
            <CategoryPill category={memory.category} />
          </div>

          {/* Content */}
          <div className="prose prose-sm prose-invert max-w-none text-sm text-foreground">
            <ReactMarkdown
              components={{
                // Keep inline-only — no block elements needed for memory content
                p: ({ children }) => <span className="block">{children}</span>,
                strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
                em: ({ children }) => <em className="italic">{children}</em>,
                code: ({ children }) => (
                  <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">{children}</code>
                ),
              }}
            >
              {memory.content}
            </ReactMarkdown>
          </div>

          {/* Bottom row: metadata */}
          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1">
            <ConfidenceBar value={memory.confidence} />

            <span className="text-[10px] text-muted-foreground" title={memory.created_at}>
              {formatRelativeTime(memory.created_at)}
            </span>

            <span className="text-[10px] text-muted-foreground">
              used {memory.access_count} {memory.access_count === 1 ? 'time' : 'times'}
            </span>

            <span className="text-[10px] text-muted-foreground">
              {memory.source_conversation_id ? 'from conversation' : 'explicit save'}
            </span>
          </div>
        </>
      )}
    </article>
  );
}

// ---------------------------------------------------------------------------
// ActionButton — small icon button used in hover actions
// ---------------------------------------------------------------------------

interface ActionButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'default' | 'destructive';
}

function ActionButton({ variant = 'default', className, children, ...props }: ActionButtonProps) {
  return (
    <button
      type="button"
      {...props}
      className={cn(
        'flex h-6 w-6 items-center justify-center rounded-md transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        'disabled:cursor-not-allowed disabled:opacity-40',
        variant === 'destructive'
          ? 'text-muted-foreground hover:bg-destructive/10 hover:text-destructive'
          : 'text-muted-foreground hover:bg-muted hover:text-foreground',
        className,
      )}
    >
      {children}
    </button>
  );
}
