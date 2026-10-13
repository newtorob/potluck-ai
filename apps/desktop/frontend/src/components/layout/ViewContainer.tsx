/**
 * ViewContainer - body region of a view.
 *
 * Wraps the scrollable content area below ViewHeader. Provides:
 *   - Consistent horizontal padding (24px) matching ViewHeader
 *   - Optional max-width constraint with auto centering (`narrow` =
 *     max-w-2xl for prose-density views like Memory & Settings;
 *     `wide` = max-w-5xl for grid views like Agents & Models;
 *     `full` = no constraint, fills available width)
 *   - Vertical scroll on overflow
 *   - Optional `grain` atmospheric texture (Hearth's tactility layer -
 *     used on full-bleed surfaces like empty states; off by default for
 *     content density views)
 *
 * Pair with ViewHeader inside a parent `flex h-full flex-col` to get the
 * standard "header + body fills" layout every view shares.
 */

import { type ReactNode } from 'react';
import { cn } from '@/components/ui/cn';

export type ViewContainerWidth = 'narrow' | 'wide' | 'full';

export interface ViewContainerProps {
  children: ReactNode;
  /** Max-width constraint. Default 'wide'. */
  width?: ViewContainerWidth;
  /** Apply the .grain atmospheric layer. Default false. */
  grain?: boolean;
  /** Override the inner padding. Default `px-6 py-5`. */
  padding?: string;
  className?: string;
}

const WIDTH_CLASS: Record<ViewContainerWidth, string> = {
  narrow: 'max-w-2xl',
  wide: 'max-w-5xl',
  full: 'max-w-none',
};

export function ViewContainer({
  children,
  width = 'wide',
  grain = false,
  padding = 'px-6 py-5',
  className,
}: ViewContainerProps) {
  return (
    <div
      className={cn(
        'flex-1 overflow-y-auto',
        grain && 'grain',
        className,
      )}
    >
      <div className={cn('mx-auto', WIDTH_CLASS[width], padding)}>
        {children}
      </div>
    </div>
  );
}
