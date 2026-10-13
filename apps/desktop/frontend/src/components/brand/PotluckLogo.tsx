/**
 * PotluckLogo - the Potluck brandmark.
 *
 * Concept: a hearth cradle holding three embers. Reads as a bowl with rising
 * sparks, a fireplace, or a communal table - directly tied to both "Potluck"
 * (a gathering where each member brings something) and the "Hearth" theme
 * (warmth at the center of a home).
 *
 * The mark is a single flat SVG drawn with `currentColor`, so it inherits
 * whatever text color it sits in. Wrap it in `text-primary` to render as
 * ember, or keep it inheriting `text-foreground` for high-contrast use.
 *
 * Usage:
 *   <PotluckLogo size={24} className="text-primary" />
 *   <PotluckWordmark size={24} />          // mark + "Potluck" wordmark
 *
 * Design notes:
 *   - The cradle is an open arc (not a closed bowl) - welcoming, not walled.
 *   - Embers form an upward triangle: middle ember slightly higher and
 *     slightly larger than the two side embers, giving the mark a soft
 *     hierarchy and a sense of gathering toward center.
 *   - Stroke uses round line caps + 2.4px on a 32px viewBox so it stays
 *     visually warm at small sizes (16–24px) and doesn't go anemic at large
 *     sizes (48–64px).
 *   - At 16px (favicon-ish), reduce the stroke and ember weight by setting
 *     `compact` - the form simplifies to a thicker cradle and a single
 *     centered ember so the mark reads cleanly at icon size.
 */

import { cn } from '@/components/ui/cn';

interface PotluckLogoProps {
  /** Rendered size in pixels. Default 24. */
  size?: number;
  /** Additional classes (typically text color). */
  className?: string;
  /** Compact/icon mode for sizes ≤16px - single centered ember + thicker arc. */
  compact?: boolean;
  /** Optional aria-label. Pass empty string for purely decorative use. */
  'aria-label'?: string;
}

export function PotluckLogo({
  size = 24,
  className,
  compact = false,
  'aria-label': ariaLabel,
}: PotluckLogoProps) {
  const labelled = ariaLabel !== undefined && ariaLabel !== '';
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={cn('shrink-0', className)}
      role={labelled ? 'img' : 'presentation'}
      aria-label={labelled ? ariaLabel : undefined}
      aria-hidden={labelled ? undefined : true}
    >
      {/* Hearth cradle - open arc, welcoming */}
      <path
        d="M5 14 C 5 22, 9 26, 16 26 C 23 26, 27 22, 27 14"
        stroke="currentColor"
        strokeWidth={compact ? 3 : 2.4}
        strokeLinecap="round"
        fill="none"
      />
      {compact ? (
        // Compact mode: single centered ember
        <circle cx="16" cy="10" r="2.6" fill="currentColor" />
      ) : (
        <>
          {/* Three embers - middle slightly higher and larger, forming an
              upward triangle that gathers toward center */}
          <circle cx="10.5" cy="11" r="1.8" fill="currentColor" />
          <circle cx="16" cy="8" r="2.2" fill="currentColor" />
          <circle cx="21.5" cy="11" r="1.8" fill="currentColor" />
        </>
      )}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// PotluckWordmark - mark + serif wordmark, used in the sidebar brand block
// ---------------------------------------------------------------------------

interface PotluckWordmarkProps {
  /** Mark size in pixels. The wordmark scales relative to the mark. */
  size?: number;
  className?: string;
}

export function PotluckWordmark({ size = 24, className }: PotluckWordmarkProps) {
  return (
    <span className={cn('inline-flex items-center gap-2', className)}>
      <PotluckLogo size={size} className="text-primary" aria-label="Potluck" />
      <span
        className="font-serif leading-none tracking-display text-foreground select-none"
        style={{ fontSize: `${size * 0.7}px` }}
      >
        Potluck
      </span>
    </span>
  );
}
