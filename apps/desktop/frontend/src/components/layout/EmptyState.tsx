/**
 * EmptyState - shared empty/zero/error state primitive.
 *
 * Standardises the serif-greeting-with-grain treatment that ChatView,
 * MemoryView, and AgentsView already use ad-hoc, and gives Settings'
 * SidecarError + future surfaces the same warmth.
 *
 * Anatomy:
 *   - Optional ember-tinted icon square (56×56, rounded-2xl, ring-1 over
 *     bg-primary/12 - matches the Hearth "P" mark sizing pattern)
 *   - Serif heading (font-serif text-2xl tracking-display)
 *   - Optional muted-foreground body copy (15px line-height-relaxed)
 *   - Primary CTA + optional secondary action slot
 *   - Grain texture layer on the surface (opt-out via `grain={false}`)
 *
 * Use this for:
 *   - "No memories yet" (MemoryView)
 *   - "No agent sessions yet" (AgentsView)
 *   - "No model loaded" (ChatView)
 *   - "Could not connect to the local sidecar" (Settings)
 *   - Any feature gate / coming-soon state
 *
 * Honesty rule: don't oversell empty states. "No memories yet - they'll
 * appear here as Potluck learns from your conversations." Not "Be the
 * first to create a memory!" That's hype-shaped.
 */

import { type ReactNode } from 'react';
import { type LucideIcon } from 'lucide-react';
import { cn } from '@/components/ui/cn';

export type EmptyStateTone = 'neutral' | 'destructive' | 'warning';

export interface EmptyStateProps {
  /** Optional Lucide icon rendered inside the ember-tinted square */
  icon?: LucideIcon;
  /** Required serif heading */
  title: ReactNode;
  /** Optional body copy under the heading */
  description?: ReactNode;
  /** Primary action button (already-styled element - slot, not props) */
  primaryAction?: ReactNode;
  /** Secondary action / link (rendered below the primary) */
  secondaryAction?: ReactNode;
  /** Tone - affects icon-square color. Default 'neutral' (ember). */
  tone?: EmptyStateTone;
  /** Apply grain texture to the surface. Default true. */
  grain?: boolean;
  /** Override outer container className */
  className?: string;
  /** Vertical alignment of the inner stack. Default 'center'. */
  align?: 'center' | 'top';
}

const TONE_BG: Record<EmptyStateTone, string> = {
  neutral: 'bg-primary/12 ring-1 ring-primary/20',
  destructive: 'bg-destructive/10 ring-1 ring-destructive/25',
  warning: 'bg-amber-500/10 ring-1 ring-amber-500/25',
};

const TONE_ICON: Record<EmptyStateTone, string> = {
  neutral: 'text-primary',
  destructive: 'text-destructive',
  // Theme-aware amber: darker on light theme for AA, brighter on dark.
  warning: 'text-amber-700 dark:text-amber-400',
};

export function EmptyState({
  icon: Icon,
  title,
  description,
  primaryAction,
  secondaryAction,
  tone = 'neutral',
  grain = true,
  className,
  align = 'center',
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        'relative flex w-full flex-col items-center gap-5 px-6 py-12 text-center',
        align === 'center' && 'flex-1 justify-center',
        grain && 'grain',
        className,
      )}
    >
      {Icon && (
        <div
          className={cn(
            'relative z-10 flex h-14 w-14 items-center justify-center rounded-2xl',
            TONE_BG[tone],
          )}
          aria-hidden="true"
        >
          <Icon className={cn('h-7 w-7', TONE_ICON[tone])} />
        </div>
      )}

      <div className="relative z-10 max-w-md space-y-2">
        <h2 className="font-serif text-2xl leading-tight tracking-display text-foreground">
          {title}
        </h2>
        {description !== undefined && (
          <p className="text-sm leading-relaxed text-muted-foreground">
            {description}
          </p>
        )}
      </div>

      {(primaryAction !== undefined || secondaryAction !== undefined) && (
        <div className="relative z-10 flex flex-col items-center gap-2">
          {primaryAction}
          {secondaryAction}
        </div>
      )}
    </div>
  );
}
