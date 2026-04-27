/**
 * CategoryLegend — horizontal legend row showing the 6 memory categories
 * and their associated canvas/graph colors.
 *
 * Used by MemoryMapView footer to decode node/point colors on both the
 * force-directed graph and the UMAP scatter.
 */

import type { MemoryCategory } from '@/types/memory';

/** Solid hex colors for canvas rendering (graph nodes and UMAP points). */
export const CATEGORY_COLORS: Record<MemoryCategory, string> = {
  preference: '#60a5fa', // blue-400
  fact: '#34d399', // emerald-400
  decision: '#a78bfa', // violet-400
  project: '#fbbf24', // amber-400
  person: '#f472b6', // pink-400
  instruction: '#f87171', // red-400
};

const CATEGORY_LABELS: Record<MemoryCategory, string> = {
  preference: 'Preference',
  fact: 'Fact',
  decision: 'Decision',
  project: 'Project',
  person: 'Person',
  instruction: 'Instruction',
};

const ORDERED_CATEGORIES: MemoryCategory[] = [
  'preference',
  'fact',
  'decision',
  'project',
  'person',
  'instruction',
];

interface CategoryLegendProps {
  className?: string;
}

export function CategoryLegend({ className }: CategoryLegendProps) {
  return (
    <div
      className={className}
      role="list"
      aria-label="Memory category colors"
    >
      {ORDERED_CATEGORIES.map((cat) => (
        <div
          key={cat}
          role="listitem"
          className="flex items-center gap-1.5"
        >
          <span
            className="h-2.5 w-2.5 shrink-0 rounded-full"
            style={{ backgroundColor: CATEGORY_COLORS[cat] }}
            aria-hidden="true"
          />
          <span className="text-xs text-muted-foreground">{CATEGORY_LABELS[cat]}</span>
        </div>
      ))}
    </div>
  );
}
