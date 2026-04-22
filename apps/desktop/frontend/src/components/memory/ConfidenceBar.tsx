/**
 * ConfidenceBar — horizontal progress bar visualizing a 0-1 confidence score.
 */

import { cn } from '@/components/ui/cn';

interface ConfidenceBarProps {
  value: number; // 0-1
  className?: string;
}

function confidenceColor(value: number): string {
  if (value >= 0.8) return 'bg-emerald-500';
  if (value >= 0.5) return 'bg-amber-500';
  return 'bg-red-500';
}

export function ConfidenceBar({ value, className }: ConfidenceBarProps) {
  const clamped = Math.min(1, Math.max(0, value));
  const pct = Math.round(clamped * 100);

  return (
    <div
      className={cn('flex items-center gap-1.5', className)}
      title={`Confidence: ${pct}%`}
      aria-label={`Confidence ${pct}%`}
    >
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-muted">
        <div
          className={cn('h-full rounded-full transition-all duration-300', confidenceColor(clamped))}
          style={{ width: `${pct}%` }}
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
        />
      </div>
      <span className="text-[10px] tabular-nums text-muted-foreground">{pct}%</span>
    </div>
  );
}
