/**
 * ModelPick — step 3 of the First Run wizard.
 *
 * Shows three tier cards (small / medium / large), pre-selecting the
 * hardware-recommended tier. User confirms → parent triggers install.
 * Shows live DownloadProgress inline on the selected card.
 */

import { CheckCircle2, AlertTriangle, XCircle } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { DownloadProgress } from '@/components/models/DownloadProgress';
import type { CatalogModel, ModelTier } from '@/stores/modelStore';
import type { HardwareInfo } from '@/hooks/useHardware';
import { inferenceVram } from '@/lib/modelFit';
import type { DownloadProgressRecord } from '@/stores/modelStore';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const TIER_LABELS: Record<ModelTier, string> = {
  small: 'Small',
  medium: 'Medium',
  large: 'Large',
};

const TIER_COLORS: Record<ModelTier, string> = {
  small: 'border-emerald-500/50 bg-emerald-500/5',
  medium: 'border-blue-500/50 bg-blue-500/5',
  large: 'border-violet-500/50 bg-violet-500/5',
};

const TIER_BADGE: Record<ModelTier, string> = {
  small: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  medium: 'bg-blue-500/10 text-blue-600 dark:text-blue-400',
  large: 'bg-violet-500/10 text-violet-600 dark:text-violet-400',
};

function formatSize(bytes: number): string {
  if (bytes >= 1_073_741_824) return `${(bytes / 1_073_741_824).toFixed(1)} GB`;
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

type Sufficiency = 'ok' | 'borderline' | 'insufficient';

function getSufficiency(model: CatalogModel, hardware: HardwareInfo | null): Sufficiency {
  if (!hardware) return 'ok'; // assume ok when hardware unknown
  const ramOk = hardware.ram_total_gb >= model.min_ram_gb;
  const usableVram = inferenceVram(hardware);
  const vramOk =
    usableVram === null || usableVram >= model.min_vram_gb;
  if (ramOk && vramOk) return 'ok';
  const ramBorder = hardware.ram_total_gb >= model.min_ram_gb * 0.8;
  const vramBorder =
    usableVram === null || usableVram >= model.min_vram_gb * 0.8;
  if (ramBorder && vramBorder) return 'borderline';
  return 'insufficient';
}

const SUFFICIENCY_ICON: Record<Sufficiency, React.ReactNode> = {
  ok: <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />,
  borderline: <AlertTriangle className="h-3.5 w-3.5 text-amber-500" />,
  insufficient: <XCircle className="h-3.5 w-3.5 text-destructive" />,
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ModelPickProps {
  /** Catalog models filtered to one per tier */
  tierModels: Record<ModelTier, CatalogModel | undefined>;
  /** The currently selected slug */
  selectedSlug: string | null;
  /** Hardware info for suitability rendering */
  hardware: HardwareInfo | null;
  /** Download progress for the selected model (if downloading) */
  downloadProgress: DownloadProgressRecord | undefined;
  /** Whether install is pending (before polling starts) */
  isInstalling: boolean;
  onSelect: (slug: string) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

const TIERS: ModelTier[] = ['small', 'medium', 'large'];

export function ModelPick({
  tierModels,
  selectedSlug,
  hardware,
  downloadProgress,
  isInstalling,
  onSelect,
}: ModelPickProps) {
  return (
    <div className="space-y-3">
      {TIERS.map((tier) => {
        const model = tierModels[tier];
        if (!model) return null;

        const isSelected = model.slug === selectedSlug;
        const sufficiency = getSufficiency(model, hardware);
        const isActiveDownload =
          isSelected &&
          (isInstalling ||
            (downloadProgress !== undefined &&
              (downloadProgress.status === 'downloading' ||
                downloadProgress.status === 'verifying')));

        return (
          <button
            key={tier}
            type="button"
            role="radio"
            aria-checked={isSelected}
            onClick={() => onSelect(model.slug)}
            disabled={isActiveDownload}
            className={cn(
              'w-full rounded-xl border-2 p-4 text-left transition-all',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
              'disabled:cursor-default',
              isSelected
                ? cn('ring-2 ring-primary', TIER_COLORS[tier])
                : 'border-border bg-card hover:border-primary/30 hover:bg-accent/50',
            )}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1 space-y-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span
                    className={cn(
                      'rounded-full px-2 py-0.5 text-xs font-medium',
                      TIER_BADGE[tier],
                    )}
                  >
                    {TIER_LABELS[tier]}
                  </span>
                  <span className="text-sm font-semibold text-foreground truncate">{model.name}</span>
                </div>
                <p className="text-xs text-muted-foreground line-clamp-2">{model.description}</p>

                <div className="flex items-center gap-2 pt-0.5">
                  <span className="text-xs text-muted-foreground">{formatSize(model.size_bytes)}</span>
                  <span className="text-muted-foreground/40">·</span>
                  <div className="flex items-center gap-1">
                    {SUFFICIENCY_ICON[sufficiency]}
                    <span className="text-xs text-muted-foreground">
                      {model.min_ram_gb} GB RAM
                      {model.min_vram_gb > 0 ? ` · ${model.min_vram_gb} GB VRAM` : ''}
                    </span>
                  </div>
                </div>
              </div>

              {/* Selection indicator */}
              <div
                className={cn(
                  'mt-0.5 h-4 w-4 shrink-0 rounded-full border-2 transition-all',
                  isSelected
                    ? 'border-primary bg-primary'
                    : 'border-muted-foreground/40 bg-transparent',
                )}
              >
                {isSelected && (
                  <div className="flex h-full w-full items-center justify-center">
                    <div className="h-1.5 w-1.5 rounded-full bg-primary-foreground" />
                  </div>
                )}
              </div>
            </div>

            {/* Download progress inline on selected card */}
            {isSelected && downloadProgress && (
              <div className="mt-3 border-t border-border/50 pt-3">
                <DownloadProgress progress={downloadProgress} />
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}
