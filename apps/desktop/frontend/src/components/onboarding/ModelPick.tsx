import { Loader2 } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { DownloadProgress } from '@/components/models/DownloadProgress';
import {
  MODEL_USES,
  MODEL_USE_LABELS,
  type ModelSuggestion,
  type ModelUse,
} from '@/lib/modelRecommendations';
import type { DownloadProgressRecord } from '@/stores/modelStore';

interface ModelPickProps {
  models: ModelSuggestion[];
  use: ModelUse;
  onUse: (use: ModelUse) => void;
  selectedSlug: string | null;
  onSelect: (slug: string) => void;
  isLoading: boolean;
  error: string | null;
  onRefresh: () => void;
  downloadProgress: DownloadProgressRecord | undefined;
  isInstalling: boolean;
}

export function ModelPick({
  models,
  use,
  onUse,
  selectedSlug,
  onSelect,
  isLoading,
  error,
  onRefresh,
  downloadProgress,
  isInstalling,
}: ModelPickProps) {
  return (
    <div className="space-y-3">
      <fieldset disabled={isInstalling} className="space-y-2">
        <legend className="text-sm font-medium">What will you mainly use it for?</legend>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {MODEL_USES.map((value) => (
            <label
              key={value}
              className={cn(
                'flex items-center gap-2 rounded-lg border px-3 py-2 text-xs cursor-pointer',
                use === value ? 'border-primary bg-primary/10' : 'border-border hover:bg-accent',
              )}
            >
              <input
                type="radio"
                name="model-use"
                value={value}
                checked={use === value}
                onChange={() => onUse(value)}
                className="accent-primary focus-visible:outline-ring"
              />
              {MODEL_USE_LABELS[value]}
            </label>
          ))}
        </div>
      </fieldset>
      {isLoading ? (
        <p role="status" className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
          <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" /> Checking models for this
          machine…
        </p>
      ) : error || models.length === 0 ? (
        <div
          role={error ? 'alert' : 'status'}
          className="rounded-xl border border-border bg-muted/30 p-4 space-y-2"
        >
          <p className="text-sm font-medium">
            {error ? 'Could not get recommendations' : 'No suitable model right now'}
          </p>
          <p className="text-xs leading-relaxed text-muted-foreground">
            {error ??
              'No model passed the memory, disk space and local check requirements. Free memory or disk space and check again, or choose a model later.'}
          </p>
          <button
            type="button"
            onClick={onRefresh}
            className="text-sm underline rounded focus-visible:outline-ring"
          >
            Check again
          </button>
        </div>
      ) : (
        <fieldset disabled={isInstalling} className="space-y-2">
          <legend className="sr-only">Suggested models, in recommended order</legend>
          {models.map((model, index) => (
            <label
              key={model.id}
              className={cn(
                'flex items-start gap-3 rounded-xl border px-3 py-2 cursor-pointer',
                selectedSlug === model.id
                  ? 'border-primary bg-primary/5'
                  : 'border-border hover:bg-accent/50',
              )}
            >
              <input
                type="radio"
                name="first-model"
                value={model.id}
                aria-label={model.name}
                aria-describedby={`suggestion-${model.id}`}
                checked={selectedSlug === model.id}
                onChange={() => onSelect(model.id)}
                className="mt-1 accent-primary focus-visible:outline-ring"
              />
              <span id={`suggestion-${model.id}`} className="min-w-0 space-y-1 block">
                <span className="flex flex-wrap gap-x-2 items-baseline">
                  <span className="font-semibold text-sm break-words">{model.name}</span>
                  {index === 0 && (
                    <span className="text-xs text-primary-text">Suggested first</span>
                  )}
                </span>
                <span className="block text-xs text-muted-foreground">
                  {(model.sizeBytes / 1024 ** 3).toFixed(1)} GiB{' '}
                  {model.installed ? 'on disk' : 'download'}
                  {' · '}
                  {model.checked ? 'Answer check passed here' : 'Estimated fit · Not checked here'}
                </span>
                {model.checked ? (
                  <span className="block text-xs text-muted-foreground">
                    {model.tokensPerSecond !== null
                      ? `${model.tokensPerSecond.toFixed(1)} tokens/s measured`
                      : 'Speed not measured'}
                    {' · '}
                    {model.contextTokens !== null
                      ? `Checked at ${model.contextTokens.toLocaleString()} context tokens`
                      : 'Context not measured'}
                  </span>
                ) : (
                  selectedSlug === model.id &&
                  model.reason && (
                    <span className="block text-xs leading-relaxed text-muted-foreground">
                      {model.reason}
                    </span>
                  )
                )}
                {use !== 'general' && (
                  <span className="block text-xs text-muted-foreground">
                    {model.taskMatch
                      ? `Catalog match for ${MODEL_USE_LABELS[use].toLowerCase()}`
                      : 'General fallback for this machine'}
                  </span>
                )}
              </span>
            </label>
          ))}
        </fieldset>
      )}
      {!isLoading && !error && models.length > 0 && (
        <p className="text-xs leading-relaxed text-muted-foreground">
          Task matches are catalog guidance, not measured quality scores. Untested models need a
          local check for speed, usable context and tool reliability.
        </p>
      )}
      {downloadProgress && (
        <div aria-live="polite">
          <DownloadProgress progress={downloadProgress} />
        </div>
      )}
    </div>
  );
}
