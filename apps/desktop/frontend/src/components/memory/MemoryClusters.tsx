/**
 * MemoryClusters — UMAP scatter plot rendered on native <canvas>.
 *
 * Data comes pre-computed from GET /memory/clusters so no client-side
 * dimensionality reduction is needed. The component handles:
 *   - Bounding-box normalisation: maps arbitrary UMAP x/y into canvas space
 *     with a fixed padding so points never touch the edge.
 *   - Mouse-wheel zoom (exponential scale, clamped 0.5x – 30x).
 *   - Click-drag pan.
 *   - Hover tooltip with content_preview, category, project.
 *   - Click → navigate to /memory?focus={id}.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { RefreshCw, AlertCircle } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import { CATEGORY_COLORS } from '@/components/memory/CategoryLegend';
import type { ClusterPoint } from '@/types/memory';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PADDING = 48; // canvas px padding around the scatter field
const MIN_RADIUS = 2;
const MAX_RADIUS = 8;
const MIN_SCALE = 0.5;
const MAX_SCALE = 30;
const ZOOM_SENSITIVITY = 0.001;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface Transform {
  scale: number;
  offsetX: number;
  offsetY: number;
}

/**
 * Map UMAP coordinates to canvas pixel space given the bounding box and the
 * current canvas dimensions. Called once to produce a stable transformer, then
 * reused for every point render and hit-test.
 */
function makeCoordMapper(
  points: ClusterPoint[],
  canvasW: number,
  canvasH: number,
): (pt: ClusterPoint) => { cx: number; cy: number } {
  if (points.length === 0) return () => ({ cx: 0, cy: 0 });

  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;

  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }

  const rangeX = maxX - minX || 1;
  const rangeY = maxY - minY || 1;
  const drawW = canvasW - PADDING * 2;
  const drawH = canvasH - PADDING * 2;

  return (pt: ClusterPoint) => ({
    cx: PADDING + ((pt.x - minX) / rangeX) * drawW,
    cy: PADDING + ((pt.y - minY) / rangeY) * drawH,
  });
}

function confidenceToRadius(confidence: number): number {
  return MIN_RADIUS + (MAX_RADIUS - MIN_RADIUS) * Math.max(0, Math.min(1, confidence));
}

function formatAge(isoString: string): string {
  const diff = Date.now() - new Date(isoString).getTime();
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface MemoryClustersProps {
  activeProject: string | null;
  /** Increment to force a refresh without unmounting */
  refreshKey?: number;
}

// ---------------------------------------------------------------------------
// Tooltip state
// ---------------------------------------------------------------------------

interface TooltipState {
  x: number; // page coords
  y: number;
  point: ClusterPoint;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MemoryClusters({ activeProject, refreshKey }: MemoryClustersProps) {
  const navigate = useNavigate();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Data
  const [points, setPoints] = useState<ClusterPoint[]>([]);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Pan/zoom transform — mutable ref to avoid re-renders on every wheel tick
  const transformRef = useRef<Transform>({ scale: 1, offsetX: 0, offsetY: 0 });
  const [, forceRedraw] = useState(0);

  // Hover tooltip
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);

  // Drag state
  const dragRef = useRef<{ startX: number; startY: number; originX: number; originY: number } | null>(null);

  // Canvas dimensions (updated on resize)
  const [canvasSize, setCanvasSize] = useState({ w: 0, h: 0 });

  // ---------------------------------------------------------------------------
  // Fetch
  // ---------------------------------------------------------------------------

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);

    localApi.memory
      .clusters(activeProject ? { project: activeProject } : {})
      .then((res) => {
        if (cancelled) return;
        setPoints(res.points);
        setGeneratedAt(res.generated_at);
        // Reset transform when new data arrives
        transformRef.current = { scale: 1, offsetX: 0, offsetY: 0 };
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load clusters');
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => { cancelled = true; };
  }, [activeProject, refreshKey]);

  // ---------------------------------------------------------------------------
  // Canvas resize observer
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
  // Draw
  // ---------------------------------------------------------------------------

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || canvasSize.w === 0 || canvasSize.h === 0) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const { scale, offsetX, offsetY } = transformRef.current;
    const mapper = makeCoordMapper(points, canvasSize.w, canvasSize.h);

    // Clear
    ctx.clearRect(0, 0, canvasSize.w, canvasSize.h);

    // Apply pan/zoom transform
    ctx.save();
    ctx.translate(offsetX, offsetY);
    ctx.scale(scale, scale);

    for (const pt of points) {
      const { cx, cy } = mapper(pt);
      const r = confidenceToRadius(pt.confidence);
      const color = CATEGORY_COLORS[pt.category];

      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.85;
      ctx.fill();
      ctx.globalAlpha = 1;

      // Stroke for visibility on dark bg
      ctx.strokeStyle = 'rgba(0,0,0,0.3)';
      ctx.lineWidth = 0.5 / scale;
      ctx.stroke();
    }

    ctx.restore();
  }, [points, canvasSize]);

  // Redraw whenever points, size, or the forceRedraw counter changes
  useEffect(() => {
    draw();
  }, [draw, points, canvasSize]);

  // ---------------------------------------------------------------------------
  // Hit test — find the nearest point under cursor (in data space)
  // ---------------------------------------------------------------------------

  const hitTest = useCallback(
    (clientX: number, clientY: number): ClusterPoint | null => {
      const canvas = canvasRef.current;
      if (!canvas) return null;
      const rect = canvas.getBoundingClientRect();
      const { scale, offsetX, offsetY } = transformRef.current;
      const mapper = makeCoordMapper(points, canvasSize.w, canvasSize.h);

      // Convert screen coords to canvas data coords
      const canvasX = (clientX - rect.left - offsetX) / scale;
      const canvasY = (clientY - rect.top - offsetY) / scale;

      let best: ClusterPoint | null = null;
      let bestDist = Infinity;

      for (const pt of points) {
        const { cx, cy } = mapper(pt);
        const r = confidenceToRadius(pt.confidence);
        const dist = Math.sqrt((canvasX - cx) ** 2 + (canvasY - cy) ** 2);
        if (dist <= Math.max(r + 4, 8) && dist < bestDist) {
          best = pt;
          bestDist = dist;
        }
      }

      return best;
    },
    [points, canvasSize],
  );

  // ---------------------------------------------------------------------------
  // Pointer events
  // ---------------------------------------------------------------------------

  const handleMouseMove = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      if (dragRef.current) {
        const dx = e.clientX - dragRef.current.startX;
        const dy = e.clientY - dragRef.current.startY;
        transformRef.current = {
          ...transformRef.current,
          offsetX: dragRef.current.originX + dx,
          offsetY: dragRef.current.originY + dy,
        };
        forceRedraw((n) => n + 1);
        draw();
        return;
      }

      const hit = hitTest(e.clientX, e.clientY);
      if (hit) {
        setTooltip({ x: e.clientX, y: e.clientY, point: hit });
      } else {
        setTooltip(null);
      }
    },
    [hitTest, draw],
  );

  const handleMouseDown = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      originX: transformRef.current.offsetX,
      originY: transformRef.current.offsetY,
    };
  }, []);

  const handleMouseUp = useCallback(() => {
    dragRef.current = null;
  }, []);

  const handleMouseLeave = useCallback(() => {
    dragRef.current = null;
    setTooltip(null);
  }, []);

  const handleClick = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      if (dragRef.current) return;
      const hit = hitTest(e.clientX, e.clientY);
      if (hit) {
        navigate(`/memory?focus=${encodeURIComponent(hit.id)}`);
      }
    },
    [hitTest, navigate],
  );

  // Attach wheel handler via native addEventListener with {passive: false} so
  // preventDefault actually works. React's onWheel prop is passive by default
  // (as of React 17+) and throws "Unable to preventDefault inside passive event
  // listener invocation" when we try to block default scroll behavior.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const mouseY = e.clientY - rect.top;

      const { scale, offsetX, offsetY } = transformRef.current;
      const delta = -e.deltaY * ZOOM_SENSITIVITY;
      const factor = Math.exp(delta * 3); // exponential feel
      const newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale * factor));

      // Zoom towards mouse cursor
      const scaleRatio = newScale / scale;
      const newOffsetX = mouseX - scaleRatio * (mouseX - offsetX);
      const newOffsetY = mouseY - scaleRatio * (mouseY - offsetY);

      transformRef.current = { scale: newScale, offsetX: newOffsetX, offsetY: newOffsetY };
      forceRedraw((n) => n + 1);
      draw();
    };

    canvas.addEventListener('wheel', onWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', onWheel);
  }, [draw]);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  const showEmptyState = !isLoading && !error && points.length < 3;
  const showCanvas = !isLoading && !error && points.length >= 3;

  return (
    <div ref={containerRef} className="relative h-full w-full overflow-hidden">
      {showCanvas && (
        <canvas
          ref={canvasRef}
          width={canvasSize.w}
          height={canvasSize.h}
          className="absolute inset-0 cursor-crosshair"
          onMouseMove={handleMouseMove}
          onMouseDown={handleMouseDown}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseLeave}
          onClick={handleClick}
          aria-label="UMAP cluster scatter plot"
          role="img"
        />
      )}

      {/* Loading / error / empty overlays — container stays mounted so the
          ResizeObserver never loses its target. */}
      {isLoading && (
        <div className="absolute inset-0 flex items-center justify-center">
          <div
            className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent"
            role="status"
            aria-label="Loading cluster data"
          />
        </div>
      )}

      {error && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3">
          <AlertCircle className="h-8 w-8 text-destructive" aria-hidden="true" />
          <p className="text-sm text-destructive">{error}</p>
        </div>
      )}

      {showEmptyState && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-muted">
            <span className="text-2xl" aria-hidden="true">🗺</span>
          </div>
          <p className="text-sm font-medium text-foreground">Not enough memories to cluster yet</p>
          <p className="text-xs text-muted-foreground">Save a few more memories to see a cluster map.</p>
        </div>
      )}

      {/* Generated-at timestamp + refresh hint */}
      {showCanvas && generatedAt && (
        <div className="pointer-events-none absolute bottom-3 right-3 flex items-center gap-1.5 rounded-lg bg-background/80 px-2.5 py-1.5 backdrop-blur-sm">
          <RefreshCw className="h-3 w-3 text-muted-foreground" aria-hidden="true" />
          <span className="text-[11px] text-muted-foreground">
            Layout as of {formatAge(generatedAt)} — refresh
          </span>
        </div>
      )}

      {/* Tooltip */}
      {tooltip && (
        <ClusterTooltip
          point={tooltip.point}
          clientX={tooltip.x}
          clientY={tooltip.y}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tooltip
// ---------------------------------------------------------------------------

interface ClusterTooltipProps {
  point: ClusterPoint;
  clientX: number;
  clientY: number;
}

function ClusterTooltip({ point, clientX, clientY }: ClusterTooltipProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Measure and flip if near viewport edge
  const style: React.CSSProperties = {
    position: 'fixed',
    left: clientX + 12,
    top: clientY - 8,
    pointerEvents: 'none',
    zIndex: 50,
  };

  return (
    <div
      ref={containerRef}
      style={style}
      className={cn(
        'max-w-[220px] rounded-lg border border-border bg-popover px-3 py-2 shadow-lg',
      )}
      role="tooltip"
    >
      <p className="line-clamp-3 text-xs text-foreground">{point.content_preview}</p>
      <div className="mt-1.5 flex items-center gap-2">
        <span
          className="h-2 w-2 shrink-0 rounded-full"
          style={{ backgroundColor: CATEGORY_COLORS[point.category] }}
          aria-hidden="true"
        />
        <span className="text-[11px] capitalize text-muted-foreground">{point.category}</span>
        {point.project && (
          <span className="truncate text-[11px] text-muted-foreground opacity-70">
            · {point.project}
          </span>
        )}
      </div>
    </div>
  );
}
