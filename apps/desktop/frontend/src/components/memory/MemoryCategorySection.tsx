/**
 * MemoryCategorySection — collapsible accordion section for a single category.
 *
 * Renders a group of MemoryCards under a category heading.
 * Collapsed by default only if there are more than 5 items; otherwise expanded.
 */

import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { MemoryCard } from './MemoryCard';
import { CATEGORY_LABELS, CATEGORY_STYLES } from './CategoryPill';
import type { Memory, MemoryCategory } from '@/types/memory';

interface MemoryCategorySectionProps {
  category: MemoryCategory;
  memories: Memory[];
}

export function MemoryCategorySection({ category, memories }: MemoryCategorySectionProps) {
  const [expanded, setExpanded] = useState(true);
  const sectionId = `memory-section-${category}`;
  const headingId = `memory-heading-${category}`;

  return (
    <section aria-labelledby={headingId}>
      {/* Accordion header */}
      <button
        type="button"
        id={headingId}
        aria-expanded={expanded}
        aria-controls={sectionId}
        onClick={() => setExpanded((v) => !v)}
        className={cn(
          'flex w-full items-center gap-2 rounded-lg px-2 py-1.5',
          'text-left transition-colors hover:bg-muted/50',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        )}
      >
        <ChevronRight
          className={cn(
            'h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform duration-150',
            expanded && 'rotate-90',
          )}
          aria-hidden="true"
        />
        <span
          className={cn(
            'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
            CATEGORY_STYLES[category],
          )}
        >
          {CATEGORY_LABELS[category]}
        </span>
        <span className="text-xs text-muted-foreground">
          {memories.length} {memories.length === 1 ? 'memory' : 'memories'}
        </span>
      </button>

      {/* Accordion body */}
      {expanded && (
        <div id={sectionId} role="region" aria-labelledby={headingId} className="mt-1.5 space-y-2 pl-2">
          {memories.map((memory) => (
            <MemoryCard key={memory.id} memory={memory} />
          ))}
        </div>
      )}
    </section>
  );
}
