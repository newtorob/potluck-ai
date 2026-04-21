/**
 * ModelCard — displays one model from the catalog with contextual actions.
 *
 * State machine:
 *   downloading  → DownloadProgress + Cancel
 *   installed    → Load + Delete
 *   active       → "Active" pill + Unload + Delete
 *   available    → Install
 *
 * Hardware sufficiency is determined by comparing the model's min_ram_gb /
 * min_vram_gb against the detected hardware. Pass `null` hardware when
 * detection hasn't completed yet — requirements are shown without colour.
 */

import { CheckCircle2, AlertTriangle, XCircle, Loader2, HardDrive } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { DownloadProgress } from './DownloadProgress';
import type { CatalogModel, ModelTier } from '@/stores/modelStore';
import type { HardwareInfo } from '@/hooks/useHardware';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatSize(bytes: number): string {
  if (bytes >= 1_073_741_824) return `${(bytes / 1_073_741_824).toFixed(1)} GB`;
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

const TIER_LABELS: Record<ModelTier, string> = {
  small: 'Small',
  medium: 'Medium',
  large: 'Large',
};

const TIER_COLORS: Record<ModelTier, string> = {
  small: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  medium: 'bg-blue-500/10 text-blue-600 dark:text-blue-400',
  large: 'bg-violet-500/10 text-violet-600 dark:text-violet-400',
};

type Sufficiency = 'ok' | 'borderline' | 'insufficient' | 'unknown';

function checkSufficiency(model: CatalogModel, hardware: HardwareInfo | null): Sufficiency {
  if (!hardware) return 'unknown';

  const ramOk = hardware.ram_total_gb >= model.min_ram_gb;
  const ramBorderline = hardware.ram_total_gb >= model.min_ram_gb * 0.8;

  // VRAM check — optional (CPU-only machines have no GPU)
  const vramOk =
    hardware.gpu_vram_gb === null || hardware.gpu_vram_gb >= model.min_vram_gb;
  const vramBorderline =
    hardware.gpu_vram_gb === null || hardware.gpu_vram_gb >= model.min_vram_gb * 0.8;

  if (ramOk && vramOk) return 'ok';
  if (ramBorderline && vramBorderline) return 'borderline';
  return 'insufficient';
}

const SUFFICIENCY_ICON: Record<Sufficiency, React.ReactNode> = {
  ok: <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" aria-label="Hardware sufficient" />,
  borderline: (
    <AlertTriangle className="h-3.5 w-3.5 text-amber-500" aria-label="Hardware borderline" />
  ),
  insufficient: (
    <XCircle className="h-3.5 w-3.5 text-destructive" aria-label="Hardware insufficient" />
  ),
  unknown: <HardDrive className="h-3.5 w-3.5 text-muted-foreground" aria-label="Hardware unknown" />,
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ModelCardProps {
  model: CatalogModel;
  isActive: boolean;
  isInstalling: boolean;
  isLoading: boolean;
  loadError?: string;
  hardware: HardwareInfo | null;
  onInstall: () => void;
  onCancelInstall: () => void;
  onLoad: () => void;
  onUnload: () => void;
  onDelete: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ModelCard({
  model,
  isActive,
  isInstalling,
  isLoading,
  loadError,
  hardware,
  onInstall,
  onCancelInstall,
  onLoad,
  onUnload,
  onDelete,
}: ModelCardProps) {
  const { slug, name, tier, description, size_bytes, min_ram_gb, min_vram_gb, installed } = model;

  const isDownloading =
    model.download_progress !== undefined &&
    (model.download_progress.status === 'downloading' ||
      model.download_progress.status === 'verifying');

  const sufficiency = checkSufficiency(model, hardware);

  return (
    <article
      className={cn(
        'flex flex-col gap-3 rounded-xl border bg-card p-5 transition-shadow',
        'hover:shadow-sm',
        isActive && 'border-primary/40 ring-1 ring-primary/20',
      )}
      aria-label={name}
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-1">
          <h3 className="truncate text-sm font-semibold text-foreground">{name}</h3>
          <div className="flex items-center gap-2">
            <span
              className={cn(
                'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium',
                TIER_COLORS[tier],
              )}
            >
              {TIER_LABELS[tier]}
            </span>
            <span className="text-xs text-muted-foreground">{formatSize(size_bytes)}</span>
          </div>
        </div>

        {/* Active pill */}
        {isActive && (
          <span className="shrink-0 inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2.5 py-0.5 text-xs font-medium text-emerald-600 dark:text-emerald-400">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
            Active
          </span>
        )}
      </div>

      {/* Description */}
      <p className="text-xs leading-relaxed text-muted-foreground line-clamp-2">{description}</p>

      {/* Requirements row */}
      <div className="flex items-center gap-1.5">
        {SUFFICIENCY_ICON[sufficiency]}
        <span className="text-xs text-muted-foreground">
          {min_ram_gb} GB RAM
          {min_vram_gb > 0 ? ` · ${min_vram_gb} GB VRAM` : ''}
        </span>
      </div>

      {/* Download progress (when in-flight) */}
      {model.download_progress && (
        <DownloadProgress
          progress={model.download_progress}
          onCancel={onCancelInstall}
        />
      )}

      {/* Load error */}
      {loadError && (
        <p className="text-xs text-destructive" role="alert">
          {loadError}
        </p>
      )}

      {/* Action buttons */}
      <div className="mt-auto flex flex-wrap gap-2">
        {!installed && !isInstalling && !isDownloading && (
          <button
            type="button"
            onClick={onInstall}
            className={cn(
              'flex-1 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              'bg-primary text-primary-foreground hover:bg-primary/90',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
            )}
            aria-label={`Install ${name}`}
          >
            Install
          </button>
        )}

        {isInstalling && !isDownloading && (
          <div
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-border px-3 py-1.5"
            aria-label="Preparing download"
          >
            <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />
            <span className="text-xs text-muted-foreground">Starting…</span>
          </div>
        )}

        {installed && !isActive && (
          <>
            <button
              type="button"
              onClick={onLoad}
              disabled={isLoading}
              className={cn(
                'flex-1 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
                'bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
              )}
              aria-label={`Load ${name}`}
            >
              {isLoading ? (
                <span className="flex items-center justify-center gap-1">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  Loading…
                </span>
              ) : (
                'Load'
              )}
            </button>
            <button
              type="button"
              onClick={onDelete}
              disabled={isLoading}
              className={cn(
                'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
                'border border-border text-muted-foreground hover:border-destructive hover:text-destructive',
                'disabled:opacity-50',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
              )}
              aria-label={`Delete ${name}`}
            >
              Delete
            </button>
          </>
        )}

        {isActive && (
          <>
            <button
              type="button"
              onClick={onUnload}
              disabled={isLoading}
              className={cn(
                'flex-1 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
                'border border-border text-muted-foreground hover:text-foreground',
                'disabled:opacity-50',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
              )}
              aria-label={`Unload ${name}`}
            >
              {isLoading ? (
                <span className="flex items-center justify-center gap-1">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  Unloading…
                </span>
              ) : (
                'Unload'
              )}
            </button>
            <button
              type="button"
              onClick={onDelete}
              disabled={isLoading}
              className={cn(
                'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
                'border border-border text-muted-foreground hover:border-destructive hover:text-destructive',
                'disabled:opacity-50',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
              )}
              aria-label={`Delete ${name}`}
            >
              Delete
            </button>
          </>
        )}
      </div>

      {/* Slug for debugging / accessibility */}
      <p className="text-xs text-muted-foreground/40 font-mono truncate">{slug}</p>
    </article>
  );
}
