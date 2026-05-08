/**
 * ModelLibrary — /models route.
 *
 * Shows the full model catalog from the Python sidecar with live download
 * progress, install/load/unload/delete actions, and a hardware summary badge.
 */

import { Settings, AlertCircle, RefreshCw } from 'lucide-react';
import { Link } from 'react-router-dom';
import { cn } from '@/components/ui/cn';
import { ModelCard } from '@/components/models/ModelCard';
import { useModels } from '@/hooks/useModels';
import { useHardware } from '@/hooks/useHardware';
import { ViewHeader } from '@/components/layout/ViewHeader';
import type { ModelTier } from '@/stores/modelStore';

// ---------------------------------------------------------------------------
// Hardware badge
// ---------------------------------------------------------------------------

const TIER_LABEL: Record<ModelTier, string> = {
  small: 'small tier',
  medium: 'medium tier',
  large: 'large tier',
};

function HardwareBadge() {
  const { hardware } = useHardware();
  if (!hardware) return null;

  const parts: string[] = [];
  if (hardware.cpu_name) parts.push(hardware.cpu_name);
  if (hardware.ram_total_gb) parts.push(`${hardware.ram_total_gb.toFixed(0)} GB`);

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="rounded-full border border-border bg-muted/50 px-3 py-1 text-xs text-muted-foreground">
        {[...parts, TIER_LABEL[hardware.recommended_tier]].join(' · ')}
      </span>
      <Link
        to="/settings#hardware"
        className="text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded"
        aria-label="Go to hardware settings"
      >
        <Settings className="inline h-3 w-3 mr-0.5" />
        Tune settings
      </Link>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Skeleton cards (loading state)
// ---------------------------------------------------------------------------

function SkeletonCard() {
  return (
    <div className="animate-fade-in rounded-xl border border-border/60 bg-card p-5 space-y-3 shadow-hearth-sm">
      <div className="h-4 w-2/3 animate-hearth-pulse rounded bg-muted" />
      <div className="h-3 w-full animate-hearth-pulse rounded bg-muted/70" />
      <div className="h-3 w-4/5 animate-hearth-pulse rounded bg-muted/70" />
      <div className="flex items-center justify-between pt-2">
        <div className="h-3 w-16 animate-hearth-pulse rounded bg-muted/70" />
        <div className="h-8 w-20 animate-hearth-pulse rounded-lg bg-muted" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ModelLibrary
// ---------------------------------------------------------------------------

export function ModelLibrary() {
  const {
    models,
    activeModelSlug,
    installing,
    loading,
    loadError,
    isFetching,
    fetchError,
    fetch,
    install,
    cancelInstall,
    remove,
    load,
    unload,
  } = useModels();

  const { hardware } = useHardware();

  return (
    <div className="flex h-full flex-col">
      <ViewHeader
        title="Models"
        count={models.length}
        actions={
          isFetching ? (
            <RefreshCw
              className="h-3.5 w-3.5 animate-spin text-muted-foreground"
              aria-label="Refreshing model list"
            />
          ) : (
            <button
              type="button"
              onClick={() => void fetch()}
              className={cn(
                'rounded-md p-1 text-muted-foreground hover:text-foreground',
                'hover:bg-accent transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              )}
              aria-label="Refresh model list"
              title="Refresh"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </button>
          )
        }
        subnav={<HardwareBadge />}
      />

      {/* Error banner */}
      {fetchError && (
        <div
          role="alert"
          className="mx-6 mt-4 flex items-start gap-2 rounded-lg border border-destructive/20 bg-destructive/5 px-4 py-3"
        >
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
          <div className="min-w-0">
            <p className="text-sm font-medium text-destructive">Failed to load models</p>
            <p className="mt-0.5 text-xs text-muted-foreground">{fetchError}</p>
          </div>
          <button
            type="button"
            onClick={() => void fetch()}
            className="ml-auto shrink-0 text-xs text-primary hover:underline"
          >
            Retry
          </button>
        </div>
      )}

      {/* Grid */}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        {isFetching && models.length === 0 ? (
          <div
            className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
            aria-label="Loading models"
          >
            {Array.from({ length: 3 }).map((_, i) => (
              <SkeletonCard key={i} />
            ))}
          </div>
        ) : (
          <div
            className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
            role="list"
            aria-label="Model catalog"
          >
            {models.map((model) => (
              <div key={model.slug} role="listitem">
                <ModelCard
                  model={model}
                  isActive={activeModelSlug === model.slug}
                  isInstalling={installing[model.slug] ?? false}
                  isLoading={loading[model.slug] ?? false}
                  {...(loadError[model.slug] !== undefined
                    ? { loadError: loadError[model.slug] }
                    : {})}
                  hardware={hardware}
                  onInstall={() => void install(model.slug)}
                  onCancelInstall={() => void cancelInstall(model.slug)}
                  onLoad={() => void load(model.slug)}
                  onUnload={() => void unload(model.slug)}
                  onDelete={() => void remove(model.slug)}
                />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
