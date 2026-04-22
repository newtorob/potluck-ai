/**
 * CategoryPill — color-coded label for a MemoryCategory.
 *
 * Colors chosen to be distinct in both light and dark modes
 * using the Tailwind palette with opacity variants.
 */

import { cn } from '@/components/ui/cn';
import type { MemoryCategory } from '@/types/memory';

const CATEGORY_STYLES: Record<MemoryCategory, string> = {
  preference: 'bg-blue-500/15 text-blue-400 border-blue-500/20',
  fact: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20',
  decision: 'bg-violet-500/15 text-violet-400 border-violet-500/20',
  project: 'bg-amber-500/15 text-amber-400 border-amber-500/20',
  person: 'bg-pink-500/15 text-pink-400 border-pink-500/20',
  instruction: 'bg-red-500/15 text-red-400 border-red-500/20',
};

const CATEGORY_LABELS: Record<MemoryCategory, string> = {
  preference: 'Preference',
  fact: 'Fact',
  decision: 'Decision',
  project: 'Project',
  person: 'Person',
  instruction: 'Instruction',
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
