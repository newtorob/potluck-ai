/**
 * ProjectPill — small badge showing the project a memory or task belongs to.
 *
 * Visual weight: deliberately quieter than CategoryPill. CategoryPill is
 * "what kind of memory is this" (essential to scanning the list),
 * ProjectPill is "scope" (helpful but secondary). They sit next to each
 * other inside MemoryCard, so they MUST read as different weights — not
 * just different colors. ProjectPill is a thin outlined pill in the warm
 * neutral palette; CategoryPill is a tinted-fill pill.
 *
 * "Global" reads even quieter (no border, no fill — just italic muted
 * text) to stay out of the way when nearly every memory is global.
 */

import { cn } from '@/components/ui/cn';

interface ProjectPillProps {
  project: string | null | undefined;
  className?: string;
}

export function ProjectPill({ project, className }: ProjectPillProps) {
  const isGlobal = project == null;

  // Global projects don't really need pill chrome — a tiny italic note
  // is enough. This frees the visual budget for CategoryPill next to it.
  if (isGlobal) {
    return (
      <span
        className={cn(
          'inline-flex items-center text-[11px] italic text-muted-foreground/70',
          className,
        )}
        title="Global: applies to all projects"
      >
        Global
      </span>
    );
  }

  return (
    <span
      className={cn(
        // Outlined neutral pill — tagged feel, not category feel.
        // Mono micro-caps to read as a "slug" / handle.
        'inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium font-mono tracking-tight',
        'border-border bg-transparent text-muted-foreground',
        'hover:border-border/80 transition-colors duration-fast',
        className,
      )}
      title={`Project: ${project}`}
    >
      {project}
    </span>
  );
}
