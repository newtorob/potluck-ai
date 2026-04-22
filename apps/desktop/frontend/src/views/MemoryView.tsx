/**
 * MemoryView — /memory route.
 *
 * Layout:
 *   - Header: "Memory" title + count + "+ New" button
 *   - Privacy banner (yellow card) if memory_enabled === false
 *   - FilterBar: category chips, debounced search, sort dropdown
 *   - Body: MemoryCards grouped by category (accordion) when unfiltered/unsorted,
 *           flat sorted list when any filter/sort/search is active
 *   - Empty state: Brain icon with descriptive text
 *   - Footer: link to /settings#privacy
 *
 * See implementation guide §9.10, §9.11.
 */

import { useState, useMemo } from 'react';
import { Brain, Plus, AlertTriangle } from 'lucide-react';
import { Link } from 'react-router-dom';
import { cn } from '@/components/ui/cn';
import { useMemories } from '@/hooks/useMemories';
import { useSettings } from '@/hooks/useSettings';
import { MemoryCard } from '@/components/memory/MemoryCard';
import { MemoryCategorySection } from '@/components/memory/MemoryCategorySection';
import { MemoryFilterBar } from '@/components/memory/MemoryFilterBar';
import { NewMemoryForm } from '@/components/memory/NewMemoryForm';
import type { Memory, MemoryCategory } from '@/types/memory';
import type { FilterState, SortKey } from '@/components/memory/MemoryFilterBar';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CATEGORY_ORDER: MemoryCategory[] = [
  'instruction',
  'preference',
  'fact',
  'decision',
  'project',
  'person',
];

// ---------------------------------------------------------------------------
// Sorting
// ---------------------------------------------------------------------------

function sortMemories(memories: Memory[], sortKey: SortKey): Memory[] {
  return [...memories].sort((a, b) => {
    switch (sortKey) {
      case 'confidence':
        return b.confidence - a.confidence;
      case 'access_count':
        return b.access_count - a.access_count;
      case 'recency':
      default:
        return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    }
  });
}

// ---------------------------------------------------------------------------
// MemoryView
// ---------------------------------------------------------------------------

const DEFAULT_FILTER: FilterState = {
  categories: [],
  search: '',
  sort: 'recency',
};

export function MemoryView() {
  const { memories, isFetching, fetchError, searchMemories } = useMemories();
  const { settings, update: updateSettings } = useSettings();

  const [showNewForm, setShowNewForm] = useState(false);
  const [filter, setFilter] = useState<FilterState>(DEFAULT_FILTER);
  const [searchResults, setSearchResults] = useState<Memory[] | null>(null);
  const [isSearching, setIsSearching] = useState(false);

  // Detect if memory is enabled in settings
  // Forward-compat: the field may not exist yet if Agent B hasn't merged
  const memoryEnabled =
    settings?.privacy &&
    'memory_enabled' in (settings.privacy as Record<string, unknown>)
      ? Boolean((settings.privacy as Record<string, unknown>)['memory_enabled'])
      : true;

  // Handle search: when query is non-empty, call /memory/search and show results
  const handleSearch = async (query: string) => {
    if (!query.trim()) {
      setSearchResults(null);
      return;
    }
    setIsSearching(true);
    try {
      const results = await searchMemories(query);
      setSearchResults(results);
    } finally {
      setIsSearching(false);
    }
  };

  const handleFilterChange = (next: FilterState) => {
    setFilter(next);
    // If search was cleared, clear search results too
    if (!next.search.trim()) setSearchResults(null);
  };

  // Determine displayed memories
  const isFiltered =
    searchResults !== null ||
    filter.categories.length > 0 ||
    filter.sort !== 'recency';

  const displayMemories = useMemo<Memory[]>(() => {
    // Use search results if we have them
    let base = searchResults !== null ? searchResults : memories;

    // Category filter
    if (filter.categories.length > 0) {
      base = base.filter((m) => filter.categories.includes(m.category));
    }

    // Sort
    return sortMemories(base, filter.sort);
  }, [memories, searchResults, filter.categories, filter.sort]);

  // Group by category for accordion view
  const grouped = useMemo<Map<MemoryCategory, Memory[]>>(() => {
    const map = new Map<MemoryCategory, Memory[]>();
    for (const cat of CATEGORY_ORDER) {
      const items = memories.filter(
        (m) => m.category === cat && (filter.categories.length === 0 || filter.categories.includes(cat)),
      );
      if (items.length > 0) {
        map.set(cat, sortMemories(items, filter.sort));
      }
    }
    return map;
  }, [memories, filter.categories, filter.sort]);

  const handleEnableMemory = () => {
    if (!settings) return;
    void updateSettings({
      privacy: {
        ...settings.privacy,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any -- forward-compat shape extension
        memory_enabled: true,
      } as any,
    });
  };

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-6 py-4">
        <div className="flex items-center gap-3">
          <Brain className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
          <h1 className="text-sm font-semibold">Memory</h1>
          {memories.length > 0 && (
            <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
              {memories.length}
            </span>
          )}
        </div>

        <button
          type="button"
          onClick={() => setShowNewForm((v) => !v)}
          aria-label="Add new memory"
          aria-expanded={showNewForm}
          className={cn(
            'flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5',
            'text-xs font-medium text-primary-foreground',
            'hover:bg-primary/90 transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          <Plus className="h-3.5 w-3.5" aria-hidden="true" />
          New
        </button>
      </div>

      {/* Scrollable body */}
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-2xl px-6 py-5 space-y-5">
          {/* Privacy disabled banner */}
          {!memoryEnabled && (
            <div
              className={cn(
                'flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10',
                'px-4 py-3',
              )}
              role="alert"
            >
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-foreground">Memory is disabled</p>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Enable memory to have Potluck remember facts, preferences, and context across
                  conversations.
                </p>
              </div>
              <button
                type="button"
                onClick={handleEnableMemory}
                className={cn(
                  'shrink-0 rounded-lg bg-amber-500 px-3 py-1.5 text-xs font-medium text-white',
                  'hover:bg-amber-400 transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                )}
              >
                Turn it on
              </button>
            </div>
          )}

          {/* New memory form */}
          {showNewForm && (
            <NewMemoryForm onClose={() => setShowNewForm(false)} />
          )}

          {/* Fetch error */}
          {fetchError && (
            <p className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive" role="alert">
              {fetchError}
            </p>
          )}

          {/* Loading skeleton */}
          {isFetching && memories.length === 0 && (
            <MemorySkeleton />
          )}

          {/* Search loading indicator */}
          {isSearching && (
            <p className="text-xs text-muted-foreground" aria-live="polite">
              Searching…
            </p>
          )}

          {/* Filter bar */}
          {!isFetching && (memories.length > 0 || searchResults !== null) && (
            <MemoryFilterBar
              value={filter}
              onChange={handleFilterChange}
              onSearch={(q) => void handleSearch(q)}
            />
          )}

          {/* Empty state */}
          {!isFetching && memories.length === 0 && !fetchError && (
            <MemoryEmptyState />
          )}

          {/* Search / filtered flat list */}
          {!isFetching && isFiltered && displayMemories.length > 0 && (
            <div className="space-y-2">
              {displayMemories.map((memory) => (
                <MemoryCard key={memory.id} memory={memory} />
              ))}
            </div>
          )}

          {/* Search empty state */}
          {!isFetching && isFiltered && displayMemories.length === 0 && memories.length > 0 && (
            <p className="text-center text-sm text-muted-foreground">
              No memories match your filter.
            </p>
          )}

          {/* Grouped accordion view (default) */}
          {!isFetching && !isFiltered && grouped.size > 0 && (
            <div className="space-y-3">
              {CATEGORY_ORDER.filter((cat) => grouped.has(cat)).map((cat) => (
                <MemoryCategorySection
                  key={cat}
                  category={cat}
                  memories={grouped.get(cat) ?? []}
                />
              ))}
            </div>
          )}

          {/* Footer */}
          <div className="border-t border-border pt-4 text-center">
            <Link
              to="/settings#privacy"
              className={cn(
                'text-xs text-muted-foreground hover:text-foreground transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded',
              )}
            >
              Manage memory in Settings
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function MemoryEmptyState() {
  return (
    <div className="flex flex-col items-center gap-4 py-16 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-muted">
        <Brain className="h-7 w-7 text-muted-foreground" aria-hidden="true" />
      </div>
      <div>
        <p className="text-sm font-medium text-foreground">No memories yet</p>
        <p className="mt-1 text-xs text-muted-foreground">
          Memories will be extracted from your conversations automatically.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Loading skeleton
// ---------------------------------------------------------------------------

function MemorySkeleton() {
  return (
    <div className="space-y-3" aria-label="Loading memories">
      {Array.from({ length: 3 }).map((_, i) => (
        <div key={i} className="rounded-xl border border-border bg-card px-4 py-3 space-y-2">
          <div className="h-3 w-1/4 animate-pulse rounded-full bg-muted" />
          <div className="h-3 w-3/4 animate-pulse rounded bg-muted" />
          <div className="h-3 w-1/2 animate-pulse rounded bg-muted" />
        </div>
      ))}
    </div>
  );
}
