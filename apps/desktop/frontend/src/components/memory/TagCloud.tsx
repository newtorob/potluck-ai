/**
 * TagCloud — collapsible keyword frequency visualization.
 *
 * Fetches GET /memory/keywords on mount and whenever activeProject changes.
 * Font size scales linearly from 12px (min count) to 24px (max count).
 * Clicking a keyword fills the search bar with that keyword and switches
 * search mode to 'exact' (the user wants literal substring matching).
 *
 * No external layout library — native button grid with inline font-size.
 */

import { useState, useEffect } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import type { KeywordFrequency } from '@/types/memory';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MIN_FONT_PX = 12;
const MAX_FONT_PX = 24;
const MAX_KEYWORDS = 50;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function scaleFontSize(count: number, minCount: number, maxCount: number): number {
  if (maxCount <= minCount) return (MIN_FONT_PX + MAX_FONT_PX) / 2;
  const ratio = (count - minCount) / (maxCount - minCount);
  return MIN_FONT_PX + ratio * (MAX_FONT_PX - MIN_FONT_PX);
}

// ---------------------------------------------------------------------------
// TagCloud
// ---------------------------------------------------------------------------

interface TagCloudProps {
  /** Currently active project — undefined means global scope */
  activeProject?: string | null;
  /** Called when the user clicks a keyword: fills search + switches to exact */
  onKeywordClick: (keyword: string) => void;
}

export function TagCloud({ activeProject, onKeywordClick }: TagCloudProps) {
  const [keywords, setKeywords] = useState<KeywordFrequency[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [expanded, setExpanded] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);

    void localApi.memory
      .keywords({
        limit: MAX_KEYWORDS,
        ...(activeProject ? { project: activeProject } : {}),
        min_count: 2,
      })
      .then((data) => {
        if (!cancelled) {
          setKeywords(data.slice(0, MAX_KEYWORDS));
          setIsLoading(false);
        }
      })
      .catch(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [activeProject]);

  // Precompute scale boundaries
  const counts = keywords.map((k) => k.count);
  const minCount = counts.length > 0 ? Math.min(...counts) : 0;
  const maxCount = counts.length > 0 ? Math.max(...counts) : 0;

  return (
    <div className="rounded-xl border border-border bg-card">
      {/* Collapsible header */}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className={cn(
          'flex w-full items-center gap-2 px-4 py-2.5 text-left',
          'hover:bg-muted/40 transition-colors rounded-xl',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          expanded && 'rounded-b-none',
        )}
      >
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        )}
        <span className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">Keywords</span>
        {keywords.length > 0 && (
          <span className="text-[10px] text-muted-foreground">{keywords.length}</span>
        )}
      </button>

      {/* Body */}
      {expanded && (
        <div className="border-t border-border px-4 py-3">
          {isLoading && (
            <div className="flex flex-wrap gap-2">
              {Array.from({ length: 12 }).map((_, i) => (
                <div
                  key={i}
                  className="h-5 animate-pulse rounded-full bg-muted"
                  style={{ width: `${40 + (i % 4) * 20}px` }}
                  aria-hidden="true"
                />
              ))}
            </div>
          )}

          {!isLoading && keywords.length === 0 && (
            <p className="text-xs text-muted-foreground">
              No keywords yet. Keywords are extracted as memories accumulate.
            </p>
          )}

          {!isLoading && keywords.length > 0 && (
            <div
              className="flex flex-wrap gap-x-3 gap-y-2"
              role="group"
              aria-label="Keyword cloud, click to search"
            >
              {keywords.map(({ keyword, count }) => {
                const fontSize = scaleFontSize(count, minCount, maxCount);
                return (
                  <button
                    key={keyword}
                    type="button"
                    onClick={() => onKeywordClick(keyword)}
                    title={`${count} ${count === 1 ? 'memory' : 'memories'}`}
                    aria-label={`Search for keyword: ${keyword} (${count} ${count === 1 ? 'memory' : 'memories'})`}
                    style={{ fontSize: `${fontSize}px`, lineHeight: 1.3 }}
                    className={cn(
                      'rounded px-1 py-0.5 font-medium text-muted-foreground',
                      'hover:text-foreground hover:bg-muted transition-colors',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    )}
                  >
                    {keyword}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
