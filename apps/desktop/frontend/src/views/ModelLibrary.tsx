import { Package } from 'lucide-react';

/**
 * Model library view — top-level route for /models.
 *
 * Placeholder scaffold. Real implementation:
 *   - Fetches model list via useModels hook -> localApi.listModels()
 *   - ModelCard per model: name, size, description, install/delete button
 *   - DownloadProgress bar for active downloads
 *   - Hardware tier badge recommending appropriate models
 */
export function ModelLibrary() {
  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-border px-6 py-4">
        <Package className="h-5 w-5 text-muted-foreground" />
        <h1 className="text-sm font-semibold">Models</h1>
      </div>

      {/* Grid skeleton */}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={i}
              className="animate-pulse rounded-xl border border-border bg-card p-5 space-y-3"
            >
              <div className="h-4 w-2/3 rounded bg-muted" />
              <div className="h-3 w-full rounded bg-muted" />
              <div className="h-3 w-4/5 rounded bg-muted" />
              <div className="flex items-center justify-between pt-2">
                <div className="h-3 w-16 rounded bg-muted" />
                <div className="h-8 w-20 rounded-lg bg-muted" />
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Empty state overlay */}
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 pointer-events-none">
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10">
          <Package className="h-7 w-7 text-primary" />
        </div>
        <p className="text-base font-medium text-foreground">Model library coming soon</p>
        <p className="text-sm text-muted-foreground">
          Browse, install, and manage local LLMs — guide §4.2
        </p>
      </div>
    </div>
  );
}
