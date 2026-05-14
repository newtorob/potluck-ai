/**
 * CategoryPill — color-coded label for a MemoryCategory.
 *
 * Each category gets a token-driven color pair: a darker swatch for the
 * light theme (AA contrast on cream) and a lighter swatch for dark.
 * The hue stays stable across themes so a "fact" always reads green-ish,
 * a "decision" always reads ember, etc. — themable without re-learning.
 *
 * Border uses the same hue at low opacity to keep the pill quiet.
 */

import { cn } from '@/components/ui/cn';
import type { MemoryCategory } from '@/types/memory';

const CATEGORY_STYLES: Record<MemoryCategory, string> = {
  // Hue 200-ish: cool blue → maps to "preference" (calm + recurring)
  preference:
    'bg-sky-500/10 text-sky-700 border-sky-500/20 ' +
    'dark:bg-sky-500/12 dark:text-sky-300 dark:border-sky-500/20',
  // Sage → "fact" (alive + grounded). Sage token is theme-aware already.
  fact:
    'bg-sage/12 text-sage border-sage/25 ' +
    'dark:bg-sage/14 dark:text-sage dark:border-sage/30',
  // Ember → "decision" (the most active, owned-by-user category)
  decision:
    'bg-primary/10 text-primary border-primary/25 ' +
    'dark:bg-primary/12 dark:text-primary dark:border-primary/30',
  // Warm amber → "project" (the "you said this is for X" tag)
  project:
    'bg-amber-500/12 text-amber-800 border-amber-500/25 ' +
    'dark:bg-amber-500/12 dark:text-amber-300 dark:border-amber-500/25',
  // Rose → "person" (warm, human)
  person:
    'bg-rose-500/10 text-rose-700 border-rose-500/20 ' +
    'dark:bg-rose-500/12 dark:text-rose-300 dark:border-rose-500/25',
  // Neutral → "instruction" (the most rule-like, lowest emotional weight)
  instruction: 'bg-muted text-muted-foreground border-border/60',
  // Violet → "reference" (points to external state)
  reference:
    'bg-violet-500/10 text-violet-700 border-violet-500/20 ' +
    'dark:bg-violet-500/12 dark:text-violet-300 dark:border-violet-500/25',
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

interface CategoryPillProps {
  category: MemoryCategory;
  className?: string;
}

export function CategoryPill({ category, className }: CategoryPillProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
        CATEGORY_STYLES[category],
        className,
      )}
    >
      {CATEGORY_LABELS[category]}
    </span>
  );
}

export { CATEGORY_STYLES, CATEGORY_LABELS };
