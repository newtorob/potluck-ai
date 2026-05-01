/**
 * CategoryLegend — horizontal legend row showing the 6 memory categories
 * and their associated canvas/graph colors.
 *
 * Used by MemoryMapView footer to decode node/point colors on both the
 * force-directed graph and the UMAP scatter.
 */

import type { MemoryCategory } from '@/types/memory';

/** Solid hex colors for canvas rendering (graph nodes and UMAP points).
 *  Saturated palette tuned for an Obsidian-style graph on a black canvas —
 *  high chroma so additive-blend glow halos read as luminous, not muddy. */
export const CATEGORY_COLORS: Record<MemoryCategory, string> = {
  preference: '#22d3ee', // cyan-400
  fact: '#84cc16', // lime-500
  decision: '#a855f7', // violet-500
  project: '#f59e0b', // amber-500
  person: '#ec4899', // pink-500
  instruction: '#ef4444', // red-500
  reference: '#14b8a6', // teal-500 — ingested vault / docs / code symbols
};

const CATEGORY_LABELS: Record<MemoryCategory, string> = {
  preference: 'Preference',
  fact: 'Fact',
  decision: 'Decision',
  project: 'Project',
  person: 'Person',
  instruction: 'Instruction',
  reference: 'Reference',
};

const ORDERED_CATEGORIES: MemoryCategory[] = [
  'preference',
  'fact',
  'decision',
  'project',
  'person',
  'instruction',
  'reference',
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
