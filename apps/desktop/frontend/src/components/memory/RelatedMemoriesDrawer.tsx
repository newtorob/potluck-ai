/**
 * RelatedMemoriesDrawer — right-side slide-in panel showing memories related
 * to a selected memory, ordered by embedding similarity.
 *
 * Navigation within the drawer replaces content in-place rather than closing
 * and reopening, so the user can explore the memory graph without losing context.
 *
 * Similarity coloring: green ≥ 0.7, amber 0.4-0.7, muted < 0.4.
 * Minimum threshold 0.35 — below this, memories are considered unrelated.
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { X, ArrowLeft } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { CategoryPill } from './CategoryPill';
import { localApi } from '@/lib/localApi';
import type { Memory, RelatedMemory } from '@/types/memory';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MIN_SIMILARITY = 0.35;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function similarityColor(s: number): string {
  if (s >= 0.7) return 'bg-sage';
  if (s >= 0.4) return 'bg-amber-500/70';
  return 'bg-muted-foreground/40';
}

function similarityLabel(s: number): string {
  if (s >= 0.7) return 'Strong match';
  if (s >= 0.4) return 'Moderate match';
  return 'Weak match';
}

// ---------------------------------------------------------------------------
// RelatedMemoriesDrawer
// ---------------------------------------------------------------------------

interface RelatedMemoriesDrawerProps {
  /** The memory the user clicked — starts the related chain */
  sourceMemory: Memory;
  onClose: () => void;
}

interface HistoryEntry {
  memory: Memory;
  related: RelatedMemory[];
}

export function RelatedMemoriesDrawer({ sourceMemory, onClose }: RelatedMemoriesDrawerProps) {
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  const currentEntry = history[history.length - 1] ?? null;

  const loadRelated = useCallback(async (memory: Memory) => {
    setIsLoading(true);
    setError(null);
    try {
      const related = await localApi.memory.related(memory.id, 8);
      const filtered = related.filter((r) => r.similarity >= MIN_SIMILARITY);
      setHistory((prev) => [...prev, { memory, related: filtered }]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load related memories');
    } finally {
      setIsLoading(false);
    }
  }, []);

  // Load the initial source memory on mount
  useEffect(() => {
    setHistory([]);
    void loadRelated(sourceMemory);
  // sourceMemory.id is the stable identity dep; the full object ref changes on re-renders
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceMemory.id]);

  // Focus close button on open for keyboard accessibility
  useEffect(() => {
    closeButtonRef.current?.focus();
  }, []);

  // Trap Escape → close drawer
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const handleNavigate = useCallback(
    (related: RelatedMemory) => {
      void loadRelated(related.memory);
    },
    [loadRelated],
  );

  const handleBack = useCallback(() => {
    setHistory((prev) => (prev.length > 1 ? prev.slice(0, -1) : prev));
  }, []);

  const canGoBack = history.length > 1;

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-30 bg-black/30"
        aria-hidden="true"
        onClick={onClose}
      />

      {/* Drawer panel */}
      <aside
        role="complementary"
        aria-label="Related memories"
        className={cn(
          'fixed right-0 top-0 z-40 flex h-full w-[420px] flex-col',
          'border-l border-border bg-background shadow-xl',
          'animate-in slide-in-from-right duration-200',
        )}
      >
        {/* Header */}
        <div className="flex items-start gap-3 border-b border-border px-4 py-3">
          {canGoBack && (
            <button
              type="button"
              onClick={handleBack}
              aria-label="Back to previous memory"
              className={cn(
                'mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md',
                'text-muted-foreground hover:bg-muted hover:text-foreground transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              )}
            >
              <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            </button>
          )}

          <div className="min-w-0 flex-1">
            <div className="mb-1.5 flex items-center gap-2">
              {currentEntry && (
                <CategoryPill category={currentEntry.memory.category} />
              )}
              <span className="font-serif text-[10px] italic text-muted-foreground tracking-hearth-label">Related memories</span>
            </div>
            <p className="line-clamp-2 text-sm text-foreground">
              {currentEntry?.memory.content ?? sourceMemory.content}
            </p>
          </div>

          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            aria-label="Close related memories drawer"
            className={cn(
              'mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md',
              'text-muted-foreground hover:bg-muted hover:text-foreground transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-4 py-3">
          {isLoading && <DrawerSkeleton />}

          {!isLoading && error && (
            <p
              className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
              role="alert"
            >
              {error}
            </p>
          )}

          {!isLoading && !error && currentEntry && currentEntry.related.length === 0 && (
            <div className="flex flex-col items-center gap-3 py-12 text-center">
              <p className="text-sm font-medium text-foreground">No strongly related memories yet.</p>
              <p className="text-xs text-muted-foreground">
                Semantic similarity is computed as more memories are added. Check back after more
                conversations have been processed.
              </p>
            </div>
          )}

          {!isLoading && !error && currentEntry && currentEntry.related.length > 0 && (
            <ol className="space-y-2" aria-label="Related memories list">
              {currentEntry.related.map(({ memory, similarity }) => (
                <li key={memory.id}>
                  <button
                    type="button"
                    onClick={() => handleNavigate({ memory, similarity })}
                    aria-label={`Navigate to related memory: ${memory.content.slice(0, 60)}`}
                    className={cn(
                      'w-full rounded-xl border border-border bg-card px-3 py-2.5 text-left',
                      'hover:border-border/80 hover:bg-card/80 transition-colors',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    )}
                  >
                    {/* Top row: category pill + similarity pip */}
                    <div className="mb-1.5 flex items-center justify-between gap-2">
                      <CategoryPill category={memory.category} />
                      <SimilarityPip value={similarity} />
                    </div>

                    {/* Content */}
                    <p className="line-clamp-3 text-xs text-foreground">{memory.content}</p>
                  </button>
                </li>
              ))}
            </ol>
          )}
        </div>
      </aside>
    </>
  );
}

// ---------------------------------------------------------------------------
// SimilarityPip
// ---------------------------------------------------------------------------

interface SimilarityPipProps {
  value: number; // 0-1
}

function SimilarityPip({ value }: SimilarityPipProps) {
  const pct = Math.round(value * 100);
  const color = similarityColor(value);
  const label = similarityLabel(value);

  return (
    <div
      className="flex items-center gap-1.5 shrink-0"
      title={`${label}: ${pct}% similar`}
      aria-label={`${label}: ${pct}% similar`}
    >
      <div className="h-1.5 w-12 overflow-hidden rounded-full bg-muted">
        <div
          className={cn('h-full rounded-full', color)}
          style={{ width: `${pct}%` }}
          role="presentation"
        />
      </div>
      <span className="text-[10px] tabular-nums text-muted-foreground">{pct}%</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DrawerSkeleton
// ---------------------------------------------------------------------------

function DrawerSkeleton() {
  return (
    <div className="space-y-2" aria-label="Loading related memories">
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="rounded-xl border border-border bg-card px-3 py-2.5 space-y-2">
          <div className="flex items-center justify-between">
            <div className="h-4 w-16 animate-pulse rounded-full bg-muted" />
            <div className="h-1.5 w-12 animate-pulse rounded-full bg-muted" />
          </div>
          <div className="h-3 w-full animate-pulse rounded bg-muted" />
          <div className="h-3 w-3/4 animate-pulse rounded bg-muted" />
        </div>
      ))}
    </div>
  );
}
