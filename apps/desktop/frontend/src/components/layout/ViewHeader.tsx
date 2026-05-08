/**
 * ViewHeader — shared chrome primitive for top-of-view headers.
 *
 * Replaces the per-view ad-hoc <header className="border-b px-6 py-4"> +
 * <h1 className="font-serif text-2xl…"> pattern. One consistent rhythm
 * across MemoryView, AgentsView, SettingsView, MembershipView, ModelLibrary,
 * TaskStateView, ChatView.
 *
 * Slots:
 *   title       — the serif page title (required)
 *   eyebrow     — optional small italic-serif kicker above the title
 *                 (e.g. "Cooperative" above "Membership")
 *   count       — optional numeric badge sitting beside the title
 *                 (e.g. "Memory 312")
 *   description — optional one-liner under the title; muted-foreground
 *   actions     — right-aligned action buttons (CTA + ancillary)
 *   tools       — left-aligned secondary controls (e.g. Chat's model
 *                 selector + parameter pills) sitting on the same row as
 *                 actions
 *   subnav      — full-width row beneath the title block (project chips,
 *                 tab strips, filter chips). Renders inside the header
 *                 border, no extra divider.
 *
 * Visual rules (Hearth):
 *   - Single bottom hairline (`border-b border-border`) — never a stacked
 *     border + shadow combo
 *   - 24px horizontal padding to match every body container
 *   - Vertical rhythm: 16px top / 14px bottom on the title row, 10px
 *     bottom on subnav row when present
 *   - Title is `font-serif` Instrument Serif; tracking-display
 *   - Count badge mirrors MemoryView's existing pill: `bg-muted` pill,
 *     `text-muted-foreground` numerals, ss01 already inherited via body
 */

import { type ReactNode } from 'react';
import { cn } from '@/components/ui/cn';

export interface ViewHeaderProps {
  /** Required page title — rendered in font-serif tracking-display */
  title: ReactNode;
  /** Optional italic-serif eyebrow above the title (e.g. "Cooperative") */
  eyebrow?: ReactNode;
  /** Optional numeric count badge beside the title */
  count?: number | null;
  /** Optional muted-foreground description under the title */
  description?: ReactNode;
  /** Right-side action buttons */
  actions?: ReactNode;
  /** Left-side secondary controls on the same row as actions */
  tools?: ReactNode;
  /** Full-width row under the title block (chips, tabs) */
  subnav?: ReactNode;
  /** Optional override className for the outer wrapper */
  className?: string;
}

export function ViewHeader({
  title,
  eyebrow,
  count,
  description,
  actions,
  tools,
  subnav,
  className,
}: ViewHeaderProps) {
  return (
    <header
      className={cn(
        'flex shrink-0 flex-col border-b border-border bg-background',
        className,
      )}
    >
      {/* Title row */}
      <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-2 px-6 pt-4 pb-3.5">
        <div className="min-w-0 flex-1">
          {eyebrow !== undefined && (
            <p
              className="mb-0.5 font-serif text-[11px] italic text-muted-foreground"
              style={{ letterSpacing: '0.04em' }}
            >
              {eyebrow}
            </p>
          )}
          <div className="flex items-baseline gap-2.5">
            <h1 className="truncate font-serif text-2xl leading-tight tracking-display text-foreground">
              {title}
            </h1>
            {typeof count === 'number' && count > 0 && (
              <span
                className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground"
                aria-label={`${count} items`}
              >
                {count.toLocaleString()}
              </span>
            )}
          </div>
          {description !== undefined && (
            <p className="mt-1 text-xs text-muted-foreground">{description}</p>
          )}
        </div>

        {(tools !== undefined || actions !== undefined) && (
          <div className="flex shrink-0 items-center gap-3">
            {tools !== undefined && (
              <div className="flex items-center gap-2">{tools}</div>
            )}
            {actions !== undefined && (
              <div className="flex items-center gap-2">{actions}</div>
            )}
          </div>
        )}
      </div>

      {/* Subnav row */}
      {subnav !== undefined && (
        <div className="px-6 pb-2.5">
          {subnav}
        </div>
      )}
    </header>
  );
}
