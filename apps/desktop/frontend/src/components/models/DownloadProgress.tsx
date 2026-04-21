/**
 * DownloadProgress — progress bar with MB downloaded, speed, and ETA.
 *
 * Handles all terminal states: verifying, error, cancelled.
 * "Cancel" button is shown during active download/verify states.
 */

import { AlertCircle, Loader2 } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import type { DownloadProgressRecord } from '@/stores/modelStore';

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function formatBytes(bytes: number): string {
  if (bytes >= 1_073_741_824) return `${(bytes / 1_073_741_824).toFixed(1)} GB`;
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

function formatSpeed(bps: number): string {
  return `${(bps / 1_048_576).toFixed(1)} MB/s`;
}

function formatEta(seconds: number): string {
  if (seconds <= 0) return '< 1s';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m === 0) return `${s}s`;
  return `${m}m ${s}s`;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface DownloadProgressProps {
  progress: DownloadProgressRecord;
  onCancel?: () => void;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function DownloadProgress({ progress, onCancel, className }: DownloadProgressProps) {
  const { status, bytes_downloaded, bytes_total, speed_bps, eta_seconds, error } = progress;

  const isActive = status === 'downloading' || status === 'verifying';
  const isError = status === 'error';
  const isCancelled = status === 'cancelled';
  const isComplete = status === 'complete';

  const pct =
    bytes_total > 0 ? Math.min(100, Math.round((bytes_downloaded / bytes_total) * 100)) : 0;

  return (
    <div className={cn('space-y-1.5', className)}>
      {/* Progress bar */}
      <div
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`Download progress: ${pct}%`}
        className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
      >
        <div
          className={cn(
            'h-full rounded-full transition-all duration-300',
            isError || isCancelled ? 'bg-destructive' : 'bg-primary',
            isActive && 'animate-pulse',
          )}
          style={{ width: `${pct}%` }}
        />
      </div>

      {/* Status row */}
      <div className="flex items-center justify-between gap-2 text-xs">
        <div className="flex items-center gap-1.5 min-w-0">
          {status === 'verifying' && (
            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-primary" />
          )}
          {isError && (
            <AlertCircle className="h-3 w-3 shrink-0 text-destructive" />
          )}

          <span
            className={cn(
              'truncate',
              isError || isCancelled ? 'text-destructive' : 'text-muted-foreground',
            )}
          >
            {isError
              ? (error ?? 'Download failed')
              : isCancelled
                ? 'Cancelled'
                : isComplete
                  ? 'Complete'
                  : status === 'verifying'
                    ? 'Verifying…'
                    : `${formatBytes(bytes_downloaded)} / ${formatBytes(bytes_total)}`}
          </span>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {status === 'downloading' && bytes_total > 0 && (
            <span className="text-muted-foreground">
              {formatSpeed(speed_bps)} · {formatEta(eta_seconds)}
            </span>
          )}

          {isActive && onCancel && (
            <button
              type="button"
              onClick={onCancel}
              className="rounded px-1.5 py-0.5 text-xs text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
              aria-label="Cancel download"
            >
              Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
