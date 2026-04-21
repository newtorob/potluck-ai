import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * Utility for merging Tailwind class names without conflicts.
 *
 * Uses clsx for conditional logic and tailwind-merge to deduplicate
 * conflicting Tailwind utilities (e.g. `p-2 p-4` → `p-4`).
 *
 * This is the standard shadcn/ui helper. All shadcn components generated
 * into this directory will use this import path.
 *
 * @example
 *   cn('px-4 py-2', isActive && 'bg-primary', className)
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
