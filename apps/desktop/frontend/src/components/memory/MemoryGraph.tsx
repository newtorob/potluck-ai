/**
 * MemoryGraph — force-directed graph of memories and their cosine-similarity edges.
 *
 * Uses react-force-graph-2d (canvas-based) loaded via React.lazy so the
 * ~50 KB bundle is never in the initial chunk.
 *
 * Node size: scaled by access_count (radius 4–14).
 * Node color: by category, matching CATEGORY_COLORS.
 * Edge width: scaled by similarity score.
 *
 * Edge-building strategy:
 *   - Fetch all memories (up to 1 000) for the active project.
 *   - Call /memory/{id}/related (limit=12) for the 200 most-recently-accessed
 *     memories. Truncation is logged and surfaced in the UI.
 *   - Include edges where similarity >= 0.6.
 *   - Dedup edges (bidirectional: store as canonical pair min(a,b)-max(a,b)).
 *
 * On node click: navigate to /memory?focus={id} (RelatedMemoriesDrawer is not
 * yet exported by Agent 2's work, so we fall back to URL navigation).
 */

import React, {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { Crosshair, AlertCircle } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import { CATEGORY_COLORS } from '@/components/memory/CategoryLegend';
import type { Memory } from '@/types/memory';

// ---------------------------------------------------------------------------
// Lazy import — keeps the 2D graph out of the initial bundle
// ---------------------------------------------------------------------------

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- react-force-graph-2d uses complex generic types; the lazy wrapper needs the cast
const ForceGraph2D = lazy(() => import('react-force-graph-2d').then((mod) => ({ default: mod.default as any })));

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const RELATED_BATCH_LIMIT = 200; // max memories we call /related for
const SIMILARITY_THRESHOLD = 0.6;
const RELATED_LIMIT = 12;
const NODE_RADIUS_MIN = 4;
const NODE_RADIUS_MAX = 14;

// ---------------------------------------------------------------------------
// Graph types
// ---------------------------------------------------------------------------

interface GraphNode {
  id: string;
  label: string;
  category: Memory['category'];
  access_count: number;
  content: string;
}

interface GraphLink {
  source: string;
  target: string;
  similarity: number;
}

interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function nodeRadius(node: GraphNode): number {
  // access_count typically 1-50+; clamp to a reasonable range
  const normalized = Math.min(node.access_count / 20, 1);
  return NODE_RADIUS_MIN + (NODE_RADIUS_MAX - NODE_RADIUS_MIN) * Math.sqrt(normalized);
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface MemoryGraphProps {
  activeProject: string | null;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MemoryGraph({ activeProject }: MemoryGraphProps) {
  const navigate = useNavigate();

  // Ref for the force-graph instance (gives us zoomToFit, centerAt)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- ForceGraphMethods generic is complex; ref holds the raw instance
  const graphRef = useRef<any>(null);

  // Containing div — measure width/height for responsive canvas
  const containerRef = useRef<HTMLDivElement>(null);
  const [canvasSize, setCanvasSize] = useState({ w: 0, h: 0 });

  // Graph state
  const [graphData, setGraphData] = useState<GraphData>({ nodes: [], links: [] });
  const [isLoading, setIsLoading] = useState(true);
  const [loadingMessage, setLoadingMessage] = useState('Loading memories…');
  const [error, setError] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);

  // Hover tooltip
  const [hoveredNode, setHoveredNode] = useState<GraphNode | null>(null);

  // Background color matching CSS var --background (dark mode default)
  // We read the computed value once so the canvas matches the page.
  const [bgColor, setBgColor] = useState('#16181f');
  useEffect(() => {
    const style = getComputedStyle(document.documentElement);
    const hsl = style.getPropertyValue('--background').trim();
    // hsl is in form "224 16% 10%" — convert to CSS hsl() string
    if (hsl) setBgColor(`hsl(${hsl})`);
  }, []);

  // ---------------------------------------------------------------------------
  // Resize observer
  // ---------------------------------------------------------------------------

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      setCanvasSize({ w: Math.floor(width), h: Math.floor(height) });
    });
    ro.observe(container);
    return () => ro.disconnect();
  }, []);

  // ---------------------------------------------------------------------------
  // Fetch memories + build graph
  // ---------------------------------------------------------------------------

  useEffect(() => {
    let cancelled = false;

    async function build() {
      setIsLoading(true);
      setError(null);
      setTruncated(false);
      setGraphData({ nodes: [], links: [] });

      try {
        setLoadingMessage('Loading memories…');
        const memories = await localApi.memory.list({
          include_all: true,
          limit: 1000,
          project: activeProject ?? undefined,
        });

        if (cancelled) return;

        if (memories.length === 0) {
          setGraphData({ nodes: [], links: [] });
          setIsLoading(false);
          return;
        }

        // Build node list
        const nodes: GraphNode[] = memories.map((m) => ({
          id: m.id,
          label: m.content.slice(0, 60),
          category: m.category,
          access_count: m.access_count,
          content: m.content,
        }));

        // Sort by recency (most recent first) and take the budget for related calls
        const sorted = [...memories].sort(
          (a, b) =>
            new Date(b.last_accessed_at).getTime() - new Date(a.last_accessed_at).getTime(),
        );

        const budget = sorted.slice(0, RELATED_BATCH_LIMIT);
        const wasTruncated = memories.length > RELATED_BATCH_LIMIT;
        if (wasTruncated) {
          setTruncated(true);
          console.info(
            `[MemoryGraph] ${memories.length} memories fetched; calling /related for the ${RELATED_BATCH_LIMIT} most-recently-accessed only.`,
          );
        }

        setLoadingMessage(`Building edges (0 / ${budget.length})…`);

        // Deduplicated edge set keyed by canonical pair
        const edgeMap = new Map<string, GraphLink>();
        const nodeIds = new Set(nodes.map((n) => n.id));

        for (let i = 0; i < budget.length; i++) {
          if (cancelled) return;

          const mem = budget[i];
          if (!mem) continue;

          try {
            const related = await localApi.memory.related(mem.id, RELATED_LIMIT);

            for (const rel of related) {
              if (rel.similarity < SIMILARITY_THRESHOLD) continue;
              if (!nodeIds.has(rel.memory.id)) continue; // target not in view

              // Canonical key: smaller id first for dedup
              const a = mem.id < rel.memory.id ? mem.id : rel.memory.id;
              const b = mem.id < rel.memory.id ? rel.memory.id : mem.id;
              const key = `${a}__${b}`;

              const existing = edgeMap.get(key);
              if (!existing || rel.similarity > existing.similarity) {
                edgeMap.set(key, { source: a, target: b, similarity: rel.similarity });
              }
            }
          } catch {
            // Individual related call failures are non-fatal; skip silently
          }

          if ((i + 1) % 20 === 0) {
            setLoadingMessage(`Building edges (${i + 1} / ${budget.length})…`);
          }
        }

        if (cancelled) return;

        setGraphData({ nodes, links: Array.from(edgeMap.values()) });
      } catch (err: unknown) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to build graph');
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    }

    void build();
    return () => { cancelled = true; };
  }, [activeProject]);

  // ---------------------------------------------------------------------------
  // zoomToFit after graph data loads
  // ---------------------------------------------------------------------------

  useEffect(() => {
    if (graphData.nodes.length > 0 && graphRef.current) {
      // Give the sim a moment to stabilize before fitting
      const t = setTimeout(() => {
        // eslint-disable-next-line @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-member-access
        graphRef.current?.zoomToFit(400, 40);
      }, 800);
      return () => clearTimeout(t);
    }
  }, [graphData]);

  // ---------------------------------------------------------------------------
  // Graph data shaped for react-force-graph-2d (stable reference via useMemo)
  // ---------------------------------------------------------------------------

  const fgData = useMemo(
    () => ({
      nodes: graphData.nodes.map((n) => ({ ...n })),
      links: graphData.links.map((l) => ({ ...l })),
    }),
    [graphData],
  );

  // ---------------------------------------------------------------------------
  // Node paint callback
  // ---------------------------------------------------------------------------

  const paintNode = useCallback(
    (node: Record<string, unknown>, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const g = node as unknown as GraphNode & { x?: number; y?: number };
      const x = g.x ?? 0;
      const y = g.y ?? 0;
      const r = nodeRadius(g);
      const color = CATEGORY_COLORS[g.category] ?? '#888';
      const isHovered = hoveredNode?.id === g.id;

      ctx.beginPath();
      ctx.arc(x, y, isHovered ? r + 2 : r, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.globalAlpha = isHovered ? 1 : 0.8;
      ctx.fill();
      ctx.globalAlpha = 1;

      if (isHovered) {
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 1.5 / globalScale;
        ctx.stroke();
      }
    },
    [hoveredNode],
  );

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  const handleNodeHover = useCallback(
    (node: Record<string, unknown> | null) => {
      setHoveredNode(node ? (node as unknown as GraphNode) : null);
    },
    [],
  );

  const handleNodeClick = useCallback(
    (node: Record<string, unknown>) => {
      const n = node as unknown as GraphNode;
      navigate(`/memory?focus=${encodeURIComponent(n.id)}`);
    },
    [navigate],
  );

  const handleRecenter = useCallback(() => {
    // eslint-disable-next-line @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-member-access
    graphRef.current?.zoomToFit(400, 40);
  }, []);

  // ---------------------------------------------------------------------------
  // Render states
  // ---------------------------------------------------------------------------

  const showEmptyState = !isLoading && !error && graphData.nodes.length === 0;
  const showGraph = !isLoading && !error && graphData.nodes.length > 0;

  return (
    <div ref={containerRef} className="relative h-full w-full overflow-hidden">
      {/* Loading / error / empty overlays — container stays mounted so the
          ResizeObserver never loses its target. */}
      {isLoading && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-20">
          <div
            className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent"
            role="status"
            aria-label="Building graph"
          />
          <p className="text-xs text-muted-foreground">{loadingMessage}</p>
        </div>
      )}

      {error && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-20">
          <AlertCircle className="h-8 w-8 text-destructive" aria-hidden="true" />
          <p className="text-sm text-destructive">{error}</p>
        </div>
      )}

      {showEmptyState && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-center z-20">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-muted">
            <span className="text-2xl" aria-hidden="true">🧠</span>
          </div>
          <p className="text-sm font-medium text-foreground">No memories to graph</p>
          <p className="text-xs text-muted-foreground">
            Add some memories and switch back to see the graph.
          </p>
        </div>
      )}

      {/* Truncation notice */}
      {showGraph && truncated && (
        <div
          className="absolute left-1/2 top-3 z-10 -translate-x-1/2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-400"
          role="status"
        >
          Showing edges for the {RELATED_BATCH_LIMIT} most-recently-accessed memories
        </div>
      )}

      {/* Recenter button */}
      {showGraph && (
        <button
          type="button"
          onClick={handleRecenter}
          aria-label="Recenter graph"
          className={cn(
            'absolute bottom-4 right-4 z-10 flex h-8 w-8 items-center justify-center rounded-lg',
            'bg-background/80 border border-border text-muted-foreground backdrop-blur-sm',
            'hover:bg-accent hover:text-foreground transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          <Crosshair className="h-4 w-4" aria-hidden="true" />
        </button>
      )}

      {/* Hover tooltip */}
      {showGraph && hoveredNode && (
        <div
          className={cn(
            'pointer-events-none absolute left-4 bottom-4 z-10 max-w-[260px]',
            'rounded-lg border border-border bg-popover px-3 py-2 shadow-lg',
          )}
          role="tooltip"
        >
          <div className="mb-1 flex items-center gap-1.5">
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ backgroundColor: CATEGORY_COLORS[hoveredNode.category] }}
              aria-hidden="true"
            />
            <span className="text-[11px] capitalize text-muted-foreground">
              {hoveredNode.category}
            </span>
          </div>
          <p className="line-clamp-4 text-xs text-foreground">{hoveredNode.content}</p>
        </div>
      )}

      {/* Force graph — Suspense fallback is the spinner */}
      {showGraph && canvasSize.w > 0 && canvasSize.h > 0 && (
        <Suspense
          fallback={
            <div className="flex h-full items-center justify-center">
              <div
                className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent"
                role="status"
                aria-label="Loading graph library"
              />
            </div>
          }
        >
          <ForceGraph2D
            ref={graphRef}
            graphData={fgData}
            width={canvasSize.w}
            height={canvasSize.h}
            backgroundColor={bgColor}
            nodeCanvasObject={paintNode}
            nodeCanvasObjectMode={() => 'replace'}
            nodeLabel={() => ''}
            nodeVal={(node: Record<string, unknown>) => {
              const n = node as unknown as GraphNode;
              return nodeRadius(n) ** 2;
            }}
            linkWidth={(link: Record<string, unknown>) => {
              const l = link as unknown as GraphLink;
              return Math.max(0.5, l.similarity * 3);
            }}
            linkColor={() => 'rgba(148,163,184,0.25)'}
            onNodeHover={handleNodeHover}
            onNodeClick={handleNodeClick}
            enableNodeDrag={true}
            enableZoomInteraction={true}
            enablePanInteraction={true}
            cooldownTicks={120}
          />
        </Suspense>
      )}
    </div>
  );
}
