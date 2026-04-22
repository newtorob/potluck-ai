/**
 * MemoryFilterBar — category multi-select chips, debounced search, and sort dropdown.
 *
 * The search box calls `onSearch` after a 300ms debounce.
 * Category chips are toggle buttons with aria-pressed.
 */

import { useState, useEffect, useRef } from 'react';
import { Search, X } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { CATEGORY_STYLES, CATEGORY_LABELS } from './CategoryPill';
import type { MemoryCategory } from '@/types/memory';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type SortKey = 'recency' | 'confidence' | 'access_count';

export interface FilterState {
  categories: MemoryCategory[];
  search: string;
  sort: SortKey;
}

interface MemoryFilterBarProps {
  value: FilterState;
  onChange: (next: FilterState) => void;
  onSearch: (query: string) => void;
}

const ALL_CATEGORIES: MemoryCategory[] = [
  'preference', 'fact', 'decision', 'project', 'person', 'instruction',
];

const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: 'recency', label: 'Most recent' },
  { value: 'confidence', label: 'Confidence' },
  { value: 'access_count', label: 'Most used' },
];

// ---------------------------------------------------------------------------
// MemoryFilterBar
// ---------------------------------------------------------------------------

export function MemoryFilterBar({ value, onChange, onSearch }: MemoryFilterBarProps) {
  const [localSearch, setLocalSearch] = useState(value.search);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Sync external search value if it's cleared from outside
  useEffect(() => {
    if (value.search === '' && localSearch !== '') {
      setLocalSearch('');
    }
    // Only sync on external clear; don't create a loop
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value.search]);

  const handleSearchChange = (raw: string) => {
    setLocalSearch(raw);
    onChange({ ...value, search: raw });

    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      onSearch(raw);
    }, 300);
  };

  const handleClearSearch = () => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    setLocalSearch('');
    onChange({ ...value, search: '' });
    onSearch('');
  };

  const toggleCategory = (cat: MemoryCategory) => {
    const next = value.categories.includes(cat)
      ? value.categories.filter((c) => c !== cat)
      : [...value.categories, cat];
    onChange({ ...value, categories: next });
  };

  return (
    <div className="space-y-2">
      {/* Search + Sort row */}
      <div className="flex items-center gap-2">
        {/* Search box */}
        <div className="relative flex-1">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <input
            type="search"
            value={localSearch}
            onChange={(e) => handleSearchChange(e.target.value)}
            placeholder="Search memories…"
            aria-label="Search memories"
            className={cn(
              'w-full rounded-lg border border-input bg-background py-2 pl-8 pr-8',
              'text-sm text-foreground placeholder:text-muted-foreground',
              'focus:outline-none focus:ring-2 focus:ring-ring',
            )}
          />
          {localSearch && (
            <button
              type="button"
              onClick={handleClearSearch}
              aria-label="Clear search"
              className={cn(
                'absolute right-2 top-1/2 -translate-y-1/2',
                'text-muted-foreground hover:text-foreground transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded',
              )}
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          )}
        </div>

        {/* Sort dropdown */}
        <select
          value={value.sort}
          onChange={(e) => onChange({ ...value, sort: e.target.value as SortKey })}
          aria-label="Sort memories by"
          className={cn(
            'rounded-lg border border-input bg-background px-3 py-2',
            'text-sm text-foreground',
            'focus:outline-none focus:ring-2 focus:ring-ring',
            'shrink-0',
          )}
        >
          {SORT_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      {/* Category chip row */}
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter by category">
        {ALL_CATEGORIES.map((cat) => {
          const active = value.categories.includes(cat);
          return (
            <button
              key={cat}
              type="button"
              role="button"
              aria-pressed={active}
              onClick={() => toggleCategory(cat)}
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
    </div>
  );
}
