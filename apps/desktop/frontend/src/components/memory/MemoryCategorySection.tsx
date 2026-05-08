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
  /** Optional: called when a card is clicked (opens related drawer) */
  onCardClick?: (memory: Memory) => void;
  /** Optional: registers a focusable ref for keyboard list navigation */
  onCardRef?: (id: string, el: HTMLElement | null) => void;
}

export function MemoryCategorySection({ category, memories, onCardClick, onCardRef }: MemoryCategorySectionProps) {
  const [expanded, setExpanded] = useState(true);
  const sectionId = `memory-section-${category}`;
  const headingId = `memory-heading-${category}`;

  return (
    <section aria-labelledby={headingId}>
      {/* Accordion header — editorial small-caps style. The serif italic
          label sets the section as a chapter, not a row. */}
      <button
        type="button"
        id={headingId}
        aria-expanded={expanded}
        aria-controls={sectionId}
        onClick={() => setExpanded((v) => !v)}
        className={cn(
          'group/header flex w-full items-center gap-2 rounded-lg px-2 py-1.5',
          'text-left transition-colors duration-fast hover:bg-muted/50',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        )}
      >
        <ChevronRight
          className={cn(
            'h-3.5 w-3.5 shrink-0 text-muted-foreground/70 transition-transform duration-base ease-out-soft',
            'group-hover/header:text-muted-foreground',
            expanded && 'rotate-90',
          )}
          aria-hidden="true"
        />
        <span
          className={cn(
            'font-serif text-[12px] italic text-foreground/85',
          )}
          style={{ letterSpacing: '0.02em' }}
        >
          {CATEGORY_LABELS[category]}
        </span>
        <span
          className={cn(
            'inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium tabular-nums',
            CATEGORY_STYLES[category],
          )}
        >
          {memories.length}
        </span>
      </button>

      {/* Accordion body — fade-in on expand */}
      {expanded && (
        <div
          id={sectionId}
          role="region"
          aria-labelledby={headingId}
          className="mt-1.5 space-y-2 pl-2 animate-fade-in"
        >
          {memories.map((memory) => (
            <div
              key={memory.id}
              ref={(el) => onCardRef?.(memory.id, el)}
              role={onCardClick ? 'button' : undefined}
              tabIndex={onCardClick ? 0 : undefined}
              aria-label={onCardClick ? `Memory: ${memory.content.slice(0, 60)}` : undefined}
              className={onCardClick ? 'cursor-pointer rounded-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring' : undefined}
              onClick={onCardClick ? () => onCardClick(memory) : undefined}
              onKeyDown={onCardClick ? (e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  onCardClick(memory);
                }
              } : undefined}
            >
              <MemoryCard memory={memory} />
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
