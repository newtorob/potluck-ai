/**
 * MemoryView — /memory route.
 *
 * Layout:
 *   - Header: "Memory" title + count + "+ New" + "View as map" link
 *   - Prominent search bar (h-12) with semantic/exact mode toggle + "/" hint
 *   - Filters row: category multi-select chips, confidence range, date range
 *   - Tag cloud (collapsible, defaults expanded)
 *   - Body: flat search results when query is set; grouped accordion view when empty
 *
 * Keyboard shortcuts (only active when /memory is mounted, and NOT when focus
 * is in a text input or textarea):
 *   /      → focus search bar
 *   Esc    → clear query + return focus to search bar (then Esc again closes if no query)
 *   ↑ / ↓  → navigate the memory list (move focus ring)
 *   Enter  → open related drawer for the focused memory card
 */

import { useState, useMemo, useEffect, useRef, useCallback } from 'react';
import { Brain, Plus, AlertTriangle, Search, X } from 'lucide-react';
import { Link, useSearchParams } from 'react-router-dom';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import { useMemories } from '@/hooks/useMemories';
import { useSettings } from '@/hooks/useSettings';
import { useProject } from '@/hooks/useProject';
import { useMemoryStore } from '@/stores/memoryStore';
import { resolveProjectFilter } from '@/lib/projectUtils';
import type { ProjectFilter } from '@/lib/projectUtils';
import { MemoryCard } from '@/components/memory/MemoryCard';
import { MemoryCategorySection } from '@/components/memory/MemoryCategorySection';
import { NewMemoryForm } from '@/components/memory/NewMemoryForm';
import { RelatedMemoriesDrawer } from '@/components/memory/RelatedMemoriesDrawer';
import { TagCloud } from '@/components/memory/TagCloud';
import { CATEGORY_STYLES, CATEGORY_LABELS } from '@/components/memory/CategoryPill';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';
import type { Memory, MemoryCategory } from '@/types/memory';
import type { SearchMode } from '@/stores/memoryStore';

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

const ALL_CATEGORIES: MemoryCategory[] = [
  'preference', 'fact', 'decision', 'project', 'person', 'instruction',
];

// ---------------------------------------------------------------------------
// Sort key
// ---------------------------------------------------------------------------

type SortKey = 'relevance' | 'recency' | 'confidence' | 'access_count';

function sortMemories(memories: Memory[], sortKey: SortKey): Memory[] {
  if (sortKey === 'relevance') return memories; // preserve search-result order
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

export function MemoryView() {
  const { projects, activeProject } = useProject();

  // Project scoping for memory list
  const [projectFilter, setProjectFilter] = useState<ProjectFilter>({ kind: 'active' });
  const { project: resolvedProject, includeAll } = resolveProjectFilter(projectFilter, activeProject);

  const { memories, isFetching, fetchError, performSearch } = useMemories({
    project: resolvedProject,
    include_all: includeAll,
  });

  // Pull search state directly from the store (avoids double-definition)
  const searchResults = useMemoryStore((s) => s.searchResults);
  const isSearching = useMemoryStore((s) => s.isSearching);
  const searchMode = useMemoryStore((s) => s.searchMode);
  const setSearchMode = useMemoryStore((s) => s.setSearchMode);
  const clearSearch = useMemoryStore((s) => s.clearSearch);

  const { settings, update: updateSettings } = useSettings();

  // UI state
  const [showNewForm, setShowNewForm] = useState(false);
  const [drawerMemory, setDrawerMemory] = useState<Memory | null>(null);

  // Filter state
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedCategories, setSelectedCategories] = useState<MemoryCategory[]>([]);
  const [confidenceMin, setConfidenceMin] = useState(0);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('recency');
  const [showMoreFilters, setShowMoreFilters] = useState(false);

  const searchInputRef = useRef<HTMLInputElement>(null);
  // Refs for keyboard-navigable memory items
  const memoryItemRefs = useRef<Map<string, HTMLElement>>(new Map());

  // ---------------------------------------------------------------------------
  // ?focus={id} URL param — set by MemoryGraph when a node is clicked.
  // Opens the related-memories drawer for the target memory and scrolls its
  // card into view. If the memory isn't in the current filtered list (e.g.
  // user clicked a graph node from a different project scope), fetch it by id
  // and open the drawer anyway so the click never feels dead.
  // ---------------------------------------------------------------------------
  const [searchParams, setSearchParams] = useSearchParams();
  const focusId = searchParams.get('focus');
  // Track which focusId we've already handled so re-renders don't re-open the
  // drawer after the user closes it.
  const handledFocusRef = useRef<string | null>(null);

  useEffect(() => {
    if (!focusId || handledFocusRef.current === focusId) return;
    if (isFetching) return; // wait for the list to load before resolving

    handledFocusRef.current = focusId;

    const found = memories.find((m) => m.id === focusId);
    if (found) {
      setDrawerMemory(found);
      requestAnimationFrame(() => {
        memoryItemRefs.current.get(focusId)?.scrollIntoView({
          block: 'center',
          behavior: 'smooth',
        });
      });
    } else {
      // Memory exists but lives outside the current project filter — fetch it
      // directly so the drawer still opens. Failure is silent; the click-from-
      // graph isn't critical enough to surface an error toast.
      void localApi.memory.get(focusId).then(setDrawerMemory).catch(() => {
        /* swallow */
      });
    }

    // Strip the param so a manual refresh doesn't re-trigger after dismiss.
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete('focus');
        return next;
      },
      { replace: true },
    );
  }, [focusId, isFetching, memories, setSearchParams]);

  // ---------------------------------------------------------------------------
  // Memory enabled detection
  // ---------------------------------------------------------------------------
  // PrivacySettings.memory_enabled is typed — no cast needed.
  // Default to true when settings haven't loaded yet so the banner
  // doesn't flash on startup.
  const memoryEnabled = settings?.privacy.memory_enabled ?? true;

  // ---------------------------------------------------------------------------
  // Search handling
  // ---------------------------------------------------------------------------

  const handleSearchInput = useCallback(
    (value: string) => {
      setSearchQuery(value);
      if (!value.trim()) {
        clearSearch();
        return;
      }
      performSearch(value, {
        project: resolvedProject,
        min_confidence: confidenceMin > 0 ? confidenceMin : undefined,
      });
    },
    [performSearch, clearSearch, resolvedProject, confidenceMin],
  );

  const handleClearSearch = useCallback(() => {
    setSearchQuery('');
    clearSearch();
    searchInputRef.current?.focus();
  }, [clearSearch]);

  // ---------------------------------------------------------------------------
  // Tag cloud keyword click — sets query and forces exact mode
  // ---------------------------------------------------------------------------

  const handleKeywordClick = useCallback(
    (keyword: string) => {
      setSearchQuery(keyword);
      setSearchMode('exact');
      // Trigger search immediately via store (bypass debounce since user
      // explicitly chose this keyword)
      void useMemoryStore.getState().performSearch(keyword, {
        project: resolvedProject,
      });
    },
    [setSearchMode, resolvedProject],
  );

  // When searchMode changes while a query is active, re-run the search
  useEffect(() => {
    if (searchQuery.trim()) {
      performSearch(searchQuery, {
        project: resolvedProject,
        min_confidence: confidenceMin > 0 ? confidenceMin : undefined,
      });
    }
    // Only re-run when mode changes, not every time performSearch ref is created
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchMode]);

  // ---------------------------------------------------------------------------
  // Displayed memories computation
  // ---------------------------------------------------------------------------

  const hasActiveSearch = searchQuery.trim() !== '';

  const displayMemories = useMemo<Memory[]>(() => {
    let base = hasActiveSearch && searchResults !== null ? searchResults : memories;

    // Category filter
    if (selectedCategories.length > 0) {
      base = base.filter((m) => selectedCategories.includes(m.category));
    }

    // Confidence filter (client-side when no active search)
    if (confidenceMin > 0) {
      base = base.filter((m) => m.confidence >= confidenceMin);
    }

    // Date range filter (client-side only)
    if (dateFrom) {
      const from = new Date(dateFrom).getTime();
      base = base.filter((m) => new Date(m.created_at).getTime() >= from);
    }
    if (dateTo) {
      // End of the selected day
      const to = new Date(dateTo).getTime() + 86_400_000;
      base = base.filter((m) => new Date(m.created_at).getTime() <= to);
    }

    return sortMemories(base, hasActiveSearch && sortKey === 'recency' ? 'relevance' : sortKey);
  }, [memories, searchResults, hasActiveSearch, selectedCategories, confidenceMin, dateFrom, dateTo, sortKey]);

  // Grouped view (used when no active search and no filters)
  const isFiltered = hasActiveSearch || selectedCategories.length > 0 || confidenceMin > 0 || dateFrom !== '' || dateTo !== '';

  const grouped = useMemo<Map<MemoryCategory, Memory[]>>(() => {
    const map = new Map<MemoryCategory, Memory[]>();
    for (const cat of CATEGORY_ORDER) {
      const items = memories.filter(
        (m) => m.category === cat && (selectedCategories.length === 0 || selectedCategories.includes(cat)),
      );
      if (items.length > 0) {
        map.set(cat, sortMemories(items, sortKey === 'relevance' ? 'recency' : sortKey));
      }
    }
    return map;
  }, [memories, selectedCategories, sortKey]);

  // ---------------------------------------------------------------------------
  // Keyboard shortcuts
  // ---------------------------------------------------------------------------

  useEffect(() => {
    function isEditableTarget(el: EventTarget | null): boolean {
      if (!el || !(el instanceof HTMLElement)) return false;
      const tag = el.tagName.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
      return el.isContentEditable;
    }

    function handleKeyDown(e: KeyboardEvent) {
      // "/" → focus search (only when not in a text field)
      if (e.key === '/' && !isEditableTarget(document.activeElement)) {
        e.preventDefault();
        searchInputRef.current?.focus();
        searchInputRef.current?.select();
        return;
      }

      // Esc → clear query (only when search input is focused)
      if (e.key === 'Escape' && document.activeElement === searchInputRef.current) {
        e.preventDefault();
        handleClearSearch();
        return;
      }

      // ↑ / ↓ → navigate memory list (when focus is on the list or search)
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        const items = Array.from(memoryItemRefs.current.values());
        if (items.length === 0) return;

        const currentIndex = items.findIndex((el) => el === document.activeElement);
        let nextIndex: number;

        if (e.key === 'ArrowDown') {
          nextIndex = currentIndex < 0 ? 0 : Math.min(currentIndex + 1, items.length - 1);
        } else {
          nextIndex = currentIndex <= 0 ? 0 : currentIndex - 1;
        }

        const nextEl = items[nextIndex];
        if (nextEl) {
          e.preventDefault();
          nextEl.focus();
        }
      }
    }

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [handleClearSearch]);

  // ---------------------------------------------------------------------------
  // Settings helpers
  // ---------------------------------------------------------------------------

  const handleEnableMemory = () => {
    if (!settings) return;
    void updateSettings({ privacy: { ...settings.privacy, memory_enabled: true } });
  };

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  // TagCloud accepts string | undefined — coerce null (global) to undefined
  const tagCloudProject = resolvedProject ?? undefined;

  return (
    <div className="flex h-full flex-col">
      {/* ------------------------------------------------------------------ */}
      {/* Unified header — title + count + actions + project subnav          */}
      {/* ------------------------------------------------------------------ */}
      <ViewHeader
        title="Memory"
        count={memories.length}
        actions={
          <>
            <Link
              to="/memory/map"
              className={cn(
                'text-xs text-muted-foreground hover:text-foreground transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded',
              )}
            >
              View as map
            </Link>

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
          </>
        }
        subnav={
          <ProjectFilterChips
            projects={projects.map((p) => p.project)}
            activeProject={activeProject}
            filter={projectFilter}
            onFilterChange={setProjectFilter}
          />
        }
      />

      {/* ------------------------------------------------------------------ */}
      {/* Scrollable body                                                     */}
      {/* ------------------------------------------------------------------ */}
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-2xl px-6 py-5 space-y-4">
          {/* Privacy disabled banner — Hearth-coherent (ember tint, not generic amber) */}
          {!memoryEnabled && (
            <div
              className={cn(
                'animate-fade-in-up flex items-start gap-3 rounded-xl border border-primary/25 bg-primary/8',
                'px-4 py-3 shadow-hearth-sm',
              )}
              role="alert"
            >
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden="true" />
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
                  'shrink-0 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
                  'hover:bg-primary/90 transition-colors duration-fast shadow-hearth-sm',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                )}
              >
                Turn it on
              </button>
            </div>
          )}

          {/* New memory form */}
          {showNewForm && (
            <NewMemoryForm
              onClose={() => setShowNewForm(false)}
              defaultProject={activeProject}
              projects={projects.map((p) => p.project)}
            />
          )}

          {/* Fetch error */}
          {fetchError && (
            <p
              className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
              role="alert"
            >
              {fetchError}
            </p>
          )}

          {/* ---------------------------------------------------------------- */}
          {/* Prominent search bar                                             */}
          {/* ---------------------------------------------------------------- */}
          <div className="space-y-3">
            <div className="relative flex items-center">
              {/* Search icon */}
              <Search
                className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden="true"
              />

              {/* Input — Hearth shadow at rest, ember ring on focus */}
              <input
                ref={searchInputRef}
                type="search"
                value={searchQuery}
                onChange={(e) => handleSearchInput(e.target.value)}
                placeholder="Search memories…"
                aria-label="Search memories"
                className={cn(
                  'h-12 w-full rounded-xl border border-input bg-card pl-10 pr-36',
                  'text-[15px] text-foreground placeholder:text-muted-foreground/80',
                  'shadow-hearth-sm',
                  'focus:outline-none focus:border-primary/40 focus:shadow-hearth focus:ring-2 focus:ring-ring/50',
                  'transition-all duration-base',
                )}
              />

              {/* Right-side controls */}
              <div className="absolute right-3 flex items-center gap-2">
                {/* Clear button */}
                {searchQuery && (
                  <button
                    type="button"
                    onClick={handleClearSearch}
                    aria-label="Clear search"
                    className={cn(
                      'text-muted-foreground hover:text-foreground transition-colors',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded',
                    )}
                  >
                    <X className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                )}

                {/* Semantic / Exact toggle */}
                <div
                  role="group"
                  aria-label="Search mode"
                  className="flex rounded-lg border border-border bg-muted p-0.5 text-xs"
                >
                  <ModeToggleButton
                    active={searchMode === 'semantic'}
                    onClick={() => setSearchMode('semantic')}
                    label="Semantic"
                  />
                  <ModeToggleButton
                    active={searchMode === 'exact'}
                    onClick={() => setSearchMode('exact')}
                    label="Exact"
                  />
                </div>

                {/* "/" hint badge */}
                {!searchQuery && (
                  <kbd
                    aria-hidden="true"
                    className="hidden rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground sm:inline-flex"
                  >
                    /
                  </kbd>
                )}
              </div>
            </div>

            {/* Sort dropdown */}
            <div className="flex items-center justify-end">
              <select
                value={sortKey}
                onChange={(e) => setSortKey(e.target.value as SortKey)}
                aria-label="Sort memories by"
                className={cn(
                  'rounded-lg border border-input bg-background px-3 py-1.5',
                  'text-xs text-foreground',
                  'focus:outline-none focus:ring-2 focus:ring-ring',
                )}
              >
                {hasActiveSearch && (
                  <option value="relevance">Relevance</option>
                )}
                <option value="recency">Recent</option>
                <option value="confidence">Confidence</option>
                <option value="access_count">Access count</option>
              </select>
            </div>
          </div>

          {/* ---------------------------------------------------------------- */}
          {/* Filters row                                                      */}
          {/* ---------------------------------------------------------------- */}
          <div className="space-y-2">
            {/* Category chips */}
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter by category">
              {ALL_CATEGORIES.map((cat) => {
                const active = selectedCategories.includes(cat);
                return (
                  <button
                    key={cat}
                    type="button"
                    aria-pressed={active}
                    onClick={() =>
                      setSelectedCategories((prev) =>
                        active ? prev.filter((c) => c !== cat) : [...prev, cat],
                      )
                    }
                    className={cn(
                      'rounded-full border px-2.5 py-0.5 text-xs font-medium transition-all',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                      active
                        ? CATEGORY_STYLES[cat]
                        : 'border-border text-muted-foreground hover:border-border/80 hover:text-foreground',
                    )}
                  >
                    {CATEGORY_LABELS[cat]}
                  </button>
                );
              })}
            </div>

            {/* "More filters" disclosure */}
            <button
              type="button"
              onClick={() => setShowMoreFilters((v) => !v)}
              aria-expanded={showMoreFilters}
              className={cn(
                'text-xs text-muted-foreground hover:text-foreground transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded',
              )}
            >
              {showMoreFilters ? '▴ Fewer filters' : '▾ More filters'}
            </button>

            {showMoreFilters && (
              <div className="space-y-3 rounded-xl border border-border bg-card px-4 py-3">
                {/* Confidence range */}
                <div>
                  <label className="mb-1 flex items-center justify-between text-xs font-medium text-muted-foreground">
                    <span>Min confidence</span>
                    <span className="tabular-nums">{Math.round(confidenceMin * 100)}%</span>
                  </label>
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={confidenceMin}
                    onChange={(e) => setConfidenceMin(Number(e.target.value))}
                    className="w-full accent-primary"
                    aria-label="Minimum confidence threshold"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={Math.round(confidenceMin * 100)}
                  />
                </div>

                {/* Date range */}
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="mb-1 block text-xs font-medium text-muted-foreground">
                      From
                    </label>
                    <input
                      type="date"
                      value={dateFrom}
                      onChange={(e) => setDateFrom(e.target.value)}
                      aria-label="Filter memories created from date"
                      className={cn(
                        'w-full rounded-lg border border-input bg-background px-3 py-1.5',
                        'text-xs text-foreground',
                        'focus:outline-none focus:ring-2 focus:ring-ring',
                      )}
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs font-medium text-muted-foreground">
                      To
                    </label>
                    <input
                      type="date"
                      value={dateTo}
                      onChange={(e) => setDateTo(e.target.value)}
                      aria-label="Filter memories created to date"
                      className={cn(
                        'w-full rounded-lg border border-input bg-background px-3 py-1.5',
                        'text-xs text-foreground',
                        'focus:outline-none focus:ring-2 focus:ring-ring',
                      )}
                    />
                  </div>
                </div>

                {/* Reset filters */}
                {(confidenceMin > 0 || dateFrom || dateTo) && (
                  <button
                    type="button"
                    onClick={() => {
                      setConfidenceMin(0);
                      setDateFrom('');
                      setDateTo('');
                    }}
                    className={cn(
                      'text-xs text-muted-foreground hover:text-foreground transition-colors',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded',
                    )}
                  >
                    Reset filters
                  </button>
                )}
              </div>
            )}
          </div>

          {/* ---------------------------------------------------------------- */}
          {/* Tag cloud                                                        */}
          {/* ---------------------------------------------------------------- */}
          <TagCloud
            activeProject={tagCloudProject}
            onKeywordClick={handleKeywordClick}
          />

          {/* ---------------------------------------------------------------- */}
          {/* Memory list                                                      */}
          {/* ---------------------------------------------------------------- */}

          {/* Loading skeleton (initial fetch) */}
          {isFetching && memories.length === 0 && <MemorySkeleton />}

          {/* Search loading indicator */}
          {isSearching && (
            <p className="text-xs text-muted-foreground" aria-live="polite">
              Searching…
            </p>
          )}

          {/* Empty state (no memories at all) */}
          {!isFetching && memories.length === 0 && !fetchError && <MemoryEmptyState />}

          {/* Flat search / filtered results */}
          {!isFetching && isFiltered && (
            <>
              {displayMemories.length > 0 ? (
                <div className="space-y-2" role="list" aria-label="Memory search results">
                  {displayMemories.map((memory, index) => (
                    <MemoryCardWrapper
                      key={memory.id}
                      memory={memory}
                      index={index}
                      onOpen={setDrawerMemory}
                      registerRef={(el) => {
                        if (el) {
                          memoryItemRefs.current.set(memory.id, el);
                        } else {
                          memoryItemRefs.current.delete(memory.id);
                        }
                      }}
                    />
                  ))}
                </div>
              ) : (
                <p className="text-center text-sm text-muted-foreground">
                  No memories match your filter.
                </p>
              )}
            </>
          )}

          {/* Grouped accordion view (default — no search, no filters) */}
          {!isFetching && !isFiltered && grouped.size > 0 && (
            <div className="space-y-3">
              {CATEGORY_ORDER.filter((cat) => grouped.has(cat)).map((cat) => (
                <MemoryCategorySectionWrapper
                  key={cat}
                  category={cat}
                  memories={grouped.get(cat) ?? []}
                  onOpenDrawer={setDrawerMemory}
                  registerRef={(id, el) => {
                    if (el) {
                      memoryItemRefs.current.set(id, el);
                    } else {
                      memoryItemRefs.current.delete(id);
                    }
                  }}
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

      {/* ------------------------------------------------------------------ */}
      {/* Related memories drawer                                            */}
      {/* ------------------------------------------------------------------ */}
      {drawerMemory && (
        <RelatedMemoriesDrawer
          sourceMemory={drawerMemory}
          onClose={() => setDrawerMemory(null)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ModeToggleButton
// ---------------------------------------------------------------------------

interface ModeToggleButtonProps {
  active: boolean;
  onClick: () => void;
  label: string;
}

function ModeToggleButton({ active, onClick, label }: ModeToggleButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        'rounded-md px-2 py-0.5 text-xs font-medium transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        active
          ? 'bg-background text-foreground shadow-sm'
          : 'text-muted-foreground hover:text-foreground',
      )}
    >
      {label}
    </button>
  );
}

// ---------------------------------------------------------------------------
// MemoryCardWrapper — makes a card keyboard-navigable and opens drawer on click
// ---------------------------------------------------------------------------

interface MemoryCardWrapperProps {
  memory: Memory;
  index: number;
  onOpen: (memory: Memory) => void;
  registerRef: (el: HTMLElement | null) => void;
}

function MemoryCardWrapper({ memory, index, onOpen, registerRef }: MemoryCardWrapperProps) {
  // Stagger fade-in across the first ~12 cards so the list lands in a wave
  // rather than a sudden snap. After that we cap it — too long a stagger
  // would feel sluggish.
  const delay = Math.min(index, 12) * 24;
  return (
    <div
      ref={registerRef}
      role="listitem"
      tabIndex={0}
      aria-label={`Memory: ${memory.content.slice(0, 60)}`}
      className={cn(
        'cursor-pointer rounded-xl',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        'animate-fade-in-up',
      )}
      style={{ animationDelay: `${delay}ms` }}
      onClick={() => onOpen(memory)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          onOpen(memory);
        }
      }}
    >
      <MemoryCard memory={memory} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// MemoryCategorySectionWrapper — makes grouped items open the drawer
// ---------------------------------------------------------------------------

interface MemoryCategorySectionWrapperProps {
  category: MemoryCategory;
  memories: Memory[];
  onOpenDrawer: (memory: Memory) => void;
  registerRef: (id: string, el: HTMLElement | null) => void;
}

function MemoryCategorySectionWrapper({
  category,
  memories,
  onOpenDrawer,
  registerRef,
}: MemoryCategorySectionWrapperProps) {
  // Wrap each card in the grouped section with a click/keyboard handler
  return (
    <div>
      <MemoryCategorySection
        category={category}
        memories={memories}
        onCardClick={onOpenDrawer}
        onCardRef={registerRef}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// ProjectFilterChips (unchanged from previous implementation)
// ---------------------------------------------------------------------------

interface ProjectFilterChipsProps {
  projects: string[];
  activeProject: string | null;
  filter: ProjectFilter;
  onFilterChange: (f: ProjectFilter) => void;
}

function ProjectFilterChips({ projects, activeProject, filter, onFilterChange }: ProjectFilterChipsProps) {
  const isTemporary = (() => {
    if (filter.kind === 'active') return false;
    if (filter.kind === 'all') return true;
    if (filter.kind === 'global') return activeProject !== null;
    if (filter.kind === 'project') return filter.name !== activeProject;
    return false;
  })();

  const chips: Array<{ label: string; filter: ProjectFilter }> = [
    { label: 'All (browse)', filter: { kind: 'all' } },
    { label: 'Global', filter: { kind: 'global' } },
    ...projects.map((name) => ({
      label: name,
      filter: { kind: 'project' as const, name },
    })),
  ];

  const isActive = (f: ProjectFilter): boolean => {
    if (filter.kind !== f.kind) return false;
    if (filter.kind === 'project' && f.kind === 'project') return filter.name === f.name;
    return true;
  };

  return (
    <div
      role="group"
      aria-label="Filter memories by project"
      className="flex flex-wrap items-center gap-1.5"
    >
      <button
        type="button"
        aria-pressed={filter.kind === 'active'}
        onClick={() => onFilterChange({ kind: 'active' })}
        className={cn(
          'shrink-0 rounded-full px-3 py-1 text-xs font-medium transition-colors',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          filter.kind === 'active'
            ? 'bg-primary/15 text-primary border border-primary/25'
            : 'bg-muted text-muted-foreground hover:bg-accent hover:text-foreground border border-transparent',
        )}
      >
        {activeProject ? activeProject : 'Global'}
        {filter.kind === 'active' && <span className="ml-1 opacity-60">(default)</span>}
      </button>

      {chips.map(({ label, filter: f }) => (
        <button
          key={label}
          type="button"
          aria-pressed={isActive(f)}
          onClick={() => onFilterChange(f)}
          className={cn(
            'shrink-0 rounded-full px-3 py-1 text-xs font-medium transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            isActive(f)
              ? 'bg-primary text-primary-foreground border border-transparent'
              : 'bg-muted text-muted-foreground hover:bg-accent hover:text-foreground border border-transparent',
          )}
        >
          {label}
        </button>
      ))}

      {isTemporary && (
        <span className="text-[10px] text-muted-foreground/60 italic">(temporary)</span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function MemoryEmptyState() {
  return (
    <EmptyState
      icon={Brain}
      title="No memories yet"
      description="Memories will appear here as Potluck learns from your conversations — facts, preferences, decisions, names. Nothing leaves your machine."
      align="top"
    />
  );
}

// ---------------------------------------------------------------------------
// Loading skeleton
// ---------------------------------------------------------------------------

function MemorySkeleton() {
  return (
    <div className="space-y-3 animate-fade-in" aria-label="Loading memories" aria-live="polite">
      {Array.from({ length: 3 }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl border border-border/60 bg-card px-4 py-3 space-y-2.5 shadow-hearth-sm"
          style={{ animationDelay: `${i * 60}ms` }}
        >
          {/* Category-pill stand-in */}
          <div className="flex items-center gap-2">
            <div className="h-4 w-16 animate-hearth-pulse rounded-full bg-muted" />
            <div className="h-4 w-12 animate-hearth-pulse rounded-full bg-muted/70" />
          </div>
          <div className="h-3 w-3/4 animate-hearth-pulse rounded bg-muted" />
          <div className="h-3 w-1/2 animate-hearth-pulse rounded bg-muted/70" />
        </div>
      ))}
    </div>
  );
}
