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

import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentType,
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
const ForceGraph2D = lazy(() => import('react-force-graph-2d').then((mod) => ({ default: mod.default as ComponentType<any> })));

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const RELATED_BATCH_LIMIT = 200; // max memories we call /related for
const SIMILARITY_THRESHOLD = 0.6;
const RELATED_LIMIT = 12;
// Wide spread between leaves and hubs gives the Obsidian starburst look —
// a leaf is a tiny dot, a hub with 20+ neighbours is large and glowy.
const NODE_RADIUS_MIN = 2;
const NODE_RADIUS_MAX = 18;

// ---------------------------------------------------------------------------
// Graph types
// ---------------------------------------------------------------------------

interface GraphNode {
  id: string;
  label: string;
  category: Memory['category'];
  access_count: number;
  content: string;
  degree: number;
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
  // Degree is the primary driver — Obsidian's vault graph is fundamentally
  // "what links to what." Access_count is a secondary nudge so a heavily-used
  // memory with few links still reads above pure leaves.
  const degreeNorm = Math.min(node.degree / 20, 1);
  const accessNorm = Math.min(node.access_count / 30, 1);
  const combined = degreeNorm * 0.8 + accessNorm * 0.2;
  return NODE_RADIUS_MIN + (NODE_RADIUS_MAX - NODE_RADIUS_MIN) * Math.sqrt(combined);
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type SourceFilter = 'all' | 'memories' | 'vault';

interface MemoryGraphProps {
  activeProject: string | null;
  sourceFilter?: SourceFilter;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MemoryGraph({ activeProject, sourceFilter = 'all' }: MemoryGraphProps) {
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

  // Pure black canvas — Obsidian-style. The graph deliberately breaks from the
  // app's --background HSL so additive-blend halos read as luminous against
  // true black rather than washing out against the dark-grey app chrome.
  const bgColor = '#000000';

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
        const allMemories = await localApi.memory.list({
          include_all: true,
          limit: 1000,
          ...(activeProject ? { project: activeProject } : {}),
        });

        if (cancelled) return;

        // Apply source-type filter at the node-set boundary. Edges to filtered-
        // out nodes are dropped automatically by the nodeIds.has() check below.
        const memories = allMemories.filter((m) => {
          if (sourceFilter === 'all') return true;
          if (sourceFilter === 'vault') return m.source_type === 'vault';
          // 'memories' = everything except vault
          return m.source_type !== 'vault';
        });

        if (memories.length === 0) {
          setGraphData({ nodes: [], links: [] });
          setIsLoading(false);
          return;
        }

        // Build node list — degree=0 initially, filled in once edges resolve
        const nodes: GraphNode[] = memories.map((m) => ({
          id: m.id,
          label: m.content.slice(0, 60),
          category: m.category,
          access_count: m.access_count,
          content: m.content,
          degree: 0,
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

        const links = Array.from(edgeMap.values());

        // Compute degree per node so radius reflects connectivity (Obsidian-style hubs).
        const degreeMap = new Map<string, number>();
        for (const l of links) {
          degreeMap.set(l.source, (degreeMap.get(l.source) ?? 0) + 1);
          degreeMap.set(l.target, (degreeMap.get(l.target) ?? 0) + 1);
        }
        for (const n of nodes) {
          n.degree = degreeMap.get(n.id) ?? 0;
        }

        setGraphData({ nodes, links });
      } catch (err: unknown) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to build graph');
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    }

    void build();
    return () => { cancelled = true; };
  }, [activeProject, sourceFilter]);

  // ---------------------------------------------------------------------------
  // zoomToFit after graph data loads
  // ---------------------------------------------------------------------------

  useEffect(() => {
    if (graphData.nodes.length > 0 && graphRef.current) {
      // Tune the d3-force simulation toward Obsidian-style starbursts:
      //   - Charge proportional to node radius — hubs push leaves out, leaves
      //     barely repel each other, so a hub with N leaves fans them into a
      //     halo instead of forming a tight ball.
      //   - Short link distance keeps each starburst compact.
      //   - Light centering pulls disconnected clusters into one canvas instead
      //     of letting them drift to the edges.
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment, @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-member-access
      const charge = graphRef.current?.d3Force('charge');
      // eslint-disable-next-line @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-member-access
      charge?.strength((n: GraphNode) => -10 - nodeRadius(n) * 4);
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment, @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-member-access
      const link = graphRef.current?.d3Force('link');
      // eslint-disable-next-line @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-member-access
      link?.distance(28);

      // Reheat the sim so the new params actually take effect.
      // eslint-disable-next-line @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-member-access
      graphRef.current?.d3ReheatSimulation?.();

      // Give the sim a moment to stabilize before fitting
      const t = setTimeout(() => {
        // eslint-disable-next-line @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-member-access
        graphRef.current?.zoomToFit(400, 40);
      }, 800);
      return () => clearTimeout(t);
    }
    return undefined;
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

      // Additive blending — overlapping halos brighten rather than occlude,
      // which is what gives Obsidian's vault graph its luminous feel.
      const prevComposite = ctx.globalCompositeOperation;
      ctx.globalCompositeOperation = 'lighter';

      // Halo: a soft outer disc at low alpha. Scales with node radius so
      // hubs glow more than leaves.
      const haloRadius = r * (isHovered ? 4 : 3);
      const grad = ctx.createRadialGradient(x, y, r * 0.5, x, y, haloRadius);
      grad.addColorStop(0, color);
      grad.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.beginPath();
      ctx.arc(x, y, haloRadius, 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.globalAlpha = isHovered ? 0.45 : 0.28;
      ctx.fill();

      // Core dot — solid, full saturation.
      ctx.globalAlpha = 1;
      ctx.beginPath();
      ctx.arc(x, y, isHovered ? r + 1.5 : r, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();

      ctx.globalCompositeOperation = prevComposite;

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
          ResizeObserver never loses its target. The graph canvas is pure
          black (Obsidian-style); these overlays sit ON the black, so they
          use a paper-tinted muted color for sufficient contrast. */}
      {isLoading && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-20 animate-fade-in">
          <div
            className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent"
            role="status"
            aria-label="Building graph"
          />
          <p className="text-xs text-paper/70 tabular-nums">{loadingMessage}</p>
        </div>
      )}

      {error && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-20 animate-fade-in">
          <AlertCircle className="h-8 w-8 text-destructive" aria-hidden="true" />
          <p className="text-sm text-destructive">{error}</p>
        </div>
      )}

      {showEmptyState && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-center z-20 animate-fade-in-up">
          {/* Sage-tinted halo placeholder — telegraphs "alive but empty" */}
          <div className="relative flex h-16 w-16 items-center justify-center">
            <div className="absolute inset-0 rounded-2xl bg-sage/20 blur-xl" aria-hidden="true" />
            <div className="relative flex h-14 w-14 items-center justify-center rounded-2xl border border-sage/30 bg-sage/10">
              <span className="text-2xl" aria-hidden="true">🧠</span>
            </div>
          </div>
          <p className="text-sm font-serif italic text-paper">No memories to graph yet</p>
          <p className="max-w-xs text-xs text-paper/60 leading-relaxed">
            As Potluck remembers things across conversations, they'll appear here as nodes — connected by what they have in common.
          </p>
        </div>
      )}

      {/* Truncation notice — paper-tinted on the black canvas */}
      {showGraph && truncated && (
        <div
          className={cn(
            'absolute left-1/2 top-3 z-10 -translate-x-1/2 animate-fade-in',
            'rounded-full border border-paper/15 bg-black/60 px-3 py-1 backdrop-blur-md',
            'text-[11px] text-paper/70 tabular-nums',
          )}
          role="status"
        >
          Showing edges for the {RELATED_BATCH_LIMIT} most-recently-accessed memories
        </div>
      )}

      {/* Recenter button — frosted on the black canvas */}
      {showGraph && (
        <button
          type="button"
          onClick={handleRecenter}
          aria-label="Recenter graph"
          title="Recenter (fit to view)"
          className={cn(
            'absolute bottom-4 right-4 z-10 flex h-9 w-9 items-center justify-center rounded-lg',
            'border border-paper/15 bg-black/50 text-paper/80 backdrop-blur-md',
            'transition-all duration-fast',
            'hover:bg-black/70 hover:text-paper hover:border-paper/30',
            'active:scale-95',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          <Crosshair className="h-4 w-4" aria-hidden="true" />
        </button>
      )}

      {/* Hover tooltip — frosted, always-on-black; uses the popover token
          shadow for elevation discipline. Animates in subtly. */}
      {showGraph && hoveredNode && (
        <div
          className={cn(
            'pointer-events-none absolute left-4 bottom-4 z-10 max-w-[280px]',
            'rounded-xl border border-paper/15 bg-black/70 backdrop-blur-md',
            'px-3.5 py-2.5 shadow-hearth-lg',
            'animate-fade-in',
          )}
          role="tooltip"
        >
          <div className="mb-1.5 flex items-center gap-1.5">
            {/* Glowing dot — matches the node's color, halos for the
                alive-on-black feel that defines this canvas */}
            <span className="relative inline-flex h-2 w-2 shrink-0">
              <span
                className="absolute inset-0 rounded-full opacity-50 blur-sm"
                style={{ backgroundColor: CATEGORY_COLORS[hoveredNode.category] }}
                aria-hidden="true"
              />
              <span
                className="relative h-2 w-2 rounded-full"
                style={{ backgroundColor: CATEGORY_COLORS[hoveredNode.category] }}
                aria-hidden="true"
              />
            </span>
            <span className="text-[10px] font-medium uppercase tracking-wider text-paper/60">
              {hoveredNode.category}
            </span>
            {hoveredNode.degree > 0 && (
              <>
                <span className="text-paper/30" aria-hidden="true">·</span>
                <span className="text-[10px] text-paper/50 tabular-nums">
                  {hoveredNode.degree} {hoveredNode.degree === 1 ? 'link' : 'links'}
                </span>
              </>
            )}
          </div>
          <p className="line-clamp-4 text-[13px] leading-relaxed text-paper/95">{hoveredNode.content}</p>
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
              return Math.max(0.3, l.similarity * 1.4);
            }}
            linkColor={() => 'rgba(180,190,210,0.12)'}
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
