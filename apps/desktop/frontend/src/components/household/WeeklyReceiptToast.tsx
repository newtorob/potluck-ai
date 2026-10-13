/**
 * WeeklyReceiptToast - a dismissible in-app banner that appears once per
 * completed week when the user's machines had contribution activity.
 *
 * Rendered in the App shell (fixed position, bottom-right). Lifecycle is
 * managed by useWeeklyReceiptToast - this component is purely presentational.
 *
 * Privacy rule: shows aggregate totals only. No per-request, per-peer, or
 * requester detail. Identical constraints as ContributionReceiptsCard.
 */

import { X } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import type { WeeklyReceiptToastData } from '@/hooks/useWeeklyReceiptToast';

// ---------------------------------------------------------------------------
// Helpers (local - avoids importing from ContributionReceiptsCard)
// ---------------------------------------------------------------------------

function formatCount(n: number): string {
  return n.toLocaleString('en-US');
}

function formatCredits(raw: string): string {
  const n = parseFloat(raw);
  if (isNaN(n)) return '0';
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface WeeklyReceiptToastProps {
  data: WeeklyReceiptToastData;
  onDismiss: () => void;
}

export function WeeklyReceiptToast({ data, onDismiss }: WeeklyReceiptToastProps) {
  const { requestsServed, creditsEarned } = data;

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={cn(
        'fixed bottom-10 right-4 z-50 flex max-w-xs items-start gap-3',
        'rounded-2xl border border-border/60 bg-card p-4 shadow-hearth-sm',
        'animate-fade-in',
      )}
    >
      <p className="min-w-0 flex-1 text-sm leading-snug text-foreground">
        Your machines helped answer{' '}
        <span className="font-medium">{formatCount(requestsServed)}</span>{' '}
        {requestsServed === 1 ? 'request' : 'requests'} last week and earned{' '}
        <span className="font-medium">{formatCredits(creditsEarned)}</span>{' '}
        credits.
      </p>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
        className={cn(
          'shrink-0 rounded-md p-0.5 text-muted-foreground',
          'hover:text-foreground transition-colors',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        )}
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </div>
  );
}
