/**
 * MemoryMapView - /memory/map route.
 *
 * Layout:
 *   - Sticky header: "Memory Map" + back-to-list link + [Graph | Clusters] tab toggle
 *     + project filter dropdown + Regenerate button (clusters view only).
 *   - Body: full-viewport height, renders <MemoryGraph> or <MemoryClusters>.
 *   - Footer: <CategoryLegend>.
 *
 * Project filter is local to this view - it does not mutate the global
 * activeProject in the settings store.
 */

import { useState, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, RefreshCw, GitFork, ScatterChart } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { useProject } from '@/hooks/useProject';
import { MemoryGraph, type SourceFilter } from '@/components/memory/MemoryGraph';
import { MemoryClusters } from '@/components/memory/MemoryClusters';
import { CategoryLegend } from '@/components/memory/CategoryLegend';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ActiveTab = 'graph' | 'clusters';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MemoryMapView() {
  const { projects, activeProject: globalActive } = useProject();

  // Local project selection - defaults to the global active project
  const [localProject, setLocalProject] = useState<string | null>(globalActive);
  const [activeTab, setActiveTab] = useState<ActiveTab>('graph');
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>('all');

  // Increment to force MemoryClusters to refetch without unmounting
  const [clusterRefreshKey, setClusterRefreshKey] = useState(0);

  const handleRegenerate = useCallback(() => {
    setClusterRefreshKey((k) => k + 1);
  }, []);

  // Sync local project when global changes (only if the user hasn't overridden)
  // We intentionally don't use useEffect here - this is a shallow default, not sync.

  const allProjectOptions: Array<{ label: string; value: string | null }> = [
    { label: 'Global', value: null },
    ...projects.map((p) => ({ label: p.project, value: p.project })),
  ];

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* ----------------------------------------------------------------- */}
      {/* Sticky header                                                      */}
      {/* ----------------------------------------------------------------- */}
      <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-border px-5 py-3">
        {/* Back link */}
        <Link
          to="/memory"
          className={cn(
            'flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs text-muted-foreground',
            'hover:bg-accent hover:text-foreground transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
          aria-label="Back to memory list"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
          Memory
        </Link>

        {/* Title */}
        <h1 className="text-sm font-semibold">Memory Map</h1>

        {/* Tab toggle */}
        <div
          role="tablist"
          aria-label="Map view"
          className="flex rounded-lg border border-border bg-muted p-0.5"
        >
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'graph'}
            onClick={() => setActiveTab('graph')}
            className={cn(
              'flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              activeTab === 'graph'
                ? 'bg-background text-foreground shadow-hearth-sm'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            <GitFork className="h-3 w-3" aria-hidden="true" />
            Graph
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'clusters'}
            onClick={() => setActiveTab('clusters')}
            className={cn(
              'flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              activeTab === 'clusters'
                ? 'bg-background text-foreground shadow-hearth-sm'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            <ScatterChart className="h-3 w-3" aria-hidden="true" />
            Clusters
          </button>
        </div>

        {/* Source filter - graph view only */}
        {activeTab === 'graph' && (
          <div
            role="radiogroup"
            aria-label="Source filter"
            className="flex rounded-lg border border-border bg-muted p-0.5"
          >
            {(['all', 'memories', 'vault'] as const).map((src) => (
              <button
                key={src}
                type="button"
                role="radio"
                aria-checked={sourceFilter === src}
                onClick={() => setSourceFilter(src)}
                className={cn(
                  'rounded-md px-2.5 py-1 text-xs font-medium transition-colors capitalize',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  sourceFilter === src
                    ? 'bg-background text-foreground shadow-sm'
                    : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {src}
              </button>
            ))}
          </div>
        )}

        {/* Spacer */}
        <div className="flex-1" />

        {/* Project filter */}
        <div className="flex items-center gap-2">
          <label htmlFor="map-project-filter" className="text-xs text-muted-foreground">
            Project
          </label>
          <select
            id="map-project-filter"
            value={localProject ?? ''}
            onChange={(e) => setLocalProject(e.target.value === '' ? null : e.target.value)}
            className={cn(
              'rounded-lg border border-input bg-background px-2.5 py-1 text-xs text-foreground',
              'focus:outline-none focus:ring-2 focus:ring-ring',
              'cursor-pointer',
            )}
          >
            {allProjectOptions.map((opt) => (
              <option key={opt.value ?? '__global__'} value={opt.value ?? ''}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>

        {/* Regenerate button - only meaningful for clusters */}
        {activeTab === 'clusters' && (
          <button
            type="button"
            onClick={handleRegenerate}
            className={cn(
              'flex items-center gap-1.5 rounded-lg border border-border px-3 py-1 text-xs',
              'text-muted-foreground hover:bg-accent hover:text-foreground transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
            aria-label="Regenerate cluster layout"
          >
            <RefreshCw className="h-3 w-3" aria-hidden="true" />
            Regenerate
          </button>
        )}
      </header>

      {/* ----------------------------------------------------------------- */}
      {/* Body                                                               */}
      {/* ----------------------------------------------------------------- */}
      <main className="relative min-h-0 flex-1" role="tabpanel">
        {activeTab === 'graph' ? (
          <MemoryGraph activeProject={localProject} sourceFilter={sourceFilter} />
        ) : (
          <MemoryClusters
            activeProject={localProject}
            refreshKey={clusterRefreshKey}
          />
        )}
      </main>

      {/* ----------------------------------------------------------------- */}
      {/* Footer legend                                                      */}
      {/* ----------------------------------------------------------------- */}
      <footer className="shrink-0 border-t border-border px-5 py-2.5">
        <CategoryLegend className="flex flex-wrap items-center gap-x-4 gap-y-1" />
      </footer>
    </div>
  );
}
