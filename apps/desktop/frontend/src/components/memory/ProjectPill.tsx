/**
 * ProjectPill — small badge showing the project a memory or task belongs to.
 *
 * "Global" (null project) renders in a muted style.
 * Named projects render in a purple accent to distinguish them from category pills.
 */

import { cn } from '@/components/ui/cn';

interface ProjectPillProps {
  project: string | null | undefined;
  className?: string;
}

export function ProjectPill({ project, className }: ProjectPillProps) {
  const isGlobal = project == null;
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
        isGlobal
          ? 'border-border/50 bg-muted/50 text-muted-foreground/60'
          : 'border-violet-500/30 bg-violet-500/10 text-violet-400',
        className,
      )}
      title={isGlobal ? 'Global — applies to all projects' : `Project: ${project}`}
    >
      {isGlobal ? 'Global' : project}
    </span>
  );
}
