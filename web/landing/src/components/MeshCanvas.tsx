'use client';

/**
 * MeshCanvas — the living mesh behind the landing hero.
 *
 * A canvas-2D constellation of member machines. Three groups mirror the
 * product's routing scopes:
 *
 *   yours    3 nodes, ember. Your own machines, always lit.
 *   trusted  5 nodes, sage. The invited circle. Lit in 'trusted' + 'pool'.
 *   pool     14 nodes, warm gray. The open network. Lit in 'pool' only.
 *
 * Inference requests render as ember pulses that travel node-to-node along
 * whatever edges the active scope permits, so toggling the scope switcher
 * visibly reroutes traffic. Group brightness eases toward its target, which
 * makes scope changes read as the network waking up rather than a hard cut.
 *
 * Performance + accessibility contract:
 *   - Single rAF loop, capped at devicePixelRatio 2.
 *   - Loop pauses when the tab is hidden or the canvas leaves the viewport.
 *   - prefers-reduced-motion renders one static frame and never animates.
 *   - Node positions are hand-placed (deterministic), only pulse routing
 *     uses randomness, so the composition is art-directed, not generative.
 */

import { useEffect, useRef } from 'react';

export type MeshScope = 'local' | 'trusted' | 'pool';

type Group = 'yours' | 'trusted' | 'pool';

interface MeshNode {
  /** Position in unit space, roughly -1..1, scaled at draw time. */
  ux: number;
  uy: number;
  group: Group;
  /** Base radius in CSS px. */
  r: number;
  /** Phase offset for the breathing glow. */
  phase: number;
  label?: string;
}

interface MeshEdge {
  a: number;
  b: number;
  /** Widest scope group this edge belongs to (pool edges light up last). */
  group: Group;
  /** Transient brightness left behind by a passing pulse. Decays. */
  heat: number;
}

interface Pulse {
  path: number[];
  seg: number;
  /** 0..1 progress along the current segment. */
  t: number;
  speed: number;
}

// ─── Hand-placed layout ───────────────────────────────────────────────────────

const NODES: ReadonlyArray<Omit<MeshNode, 'phase'>> = [
  // yours (0-2)
  { ux: -0.11, uy: -0.07, group: 'yours', r: 5.5, label: 'your laptop' },
  { ux: 0.08, uy: 0.02, group: 'yours', r: 6.0, label: 'your desktop' },
  { ux: -0.03, uy: 0.13, group: 'yours', r: 5.0, label: 'homelab gpu' },
  // trusted (3-7)
  { ux: 0.33, uy: -0.21, group: 'trusted', r: 4.5, label: 'a friend’s rig' },
  { ux: -0.37, uy: -0.17, group: 'trusted', r: 4.0 },
  { ux: 0.43, uy: 0.17, group: 'trusted', r: 4.5 },
  { ux: -0.31, uy: 0.31, group: 'trusted', r: 4.0 },
  { ux: 0.05, uy: -0.41, group: 'trusted', r: 4.0 },
  // pool (8-21)
  { ux: 0.63, uy: -0.45, group: 'pool', r: 3.0 },
  { ux: 0.80, uy: -0.10, group: 'pool', r: 3.5 },
  { ux: 0.86, uy: 0.29, group: 'pool', r: 3.0 },
  { ux: 0.60, uy: 0.54, group: 'pool', r: 3.5 },
  { ux: 0.27, uy: 0.71, group: 'pool', r: 3.0 },
  { ux: -0.11, uy: 0.79, group: 'pool', r: 3.5 },
  { ux: -0.49, uy: 0.63, group: 'pool', r: 3.0 },
  { ux: -0.76, uy: 0.34, group: 'pool', r: 3.5 },
  { ux: -0.87, uy: -0.05, group: 'pool', r: 3.0 },
  { ux: -0.69, uy: -0.39, group: 'pool', r: 3.5 },
  { ux: -0.38, uy: -0.63, group: 'pool', r: 3.0 },
  { ux: 0.01, uy: -0.75, group: 'pool', r: 3.5 },
  { ux: 0.35, uy: -0.65, group: 'pool', r: 3.0 },
  { ux: 0.94, uy: -0.38, group: 'pool', r: 2.5 },
];

// [a, b, group] — group decides which scope lights the edge.
const EDGES: ReadonlyArray<readonly [number, number, Group]> = [
  // yours: fully meshed
  [0, 1, 'yours'],
  [1, 2, 'yours'],
  [2, 0, 'yours'],
  // trusted spokes into yours + a partial ring
  [3, 1, 'trusted'],
  [3, 0, 'trusted'],
  [4, 0, 'trusted'],
  [5, 1, 'trusted'],
  [6, 2, 'trusted'],
  [7, 0, 'trusted'],
  [3, 5, 'trusted'],
  [4, 6, 'trusted'],
  [7, 3, 'trusted'],
  [7, 4, 'trusted'],
  // pool: loose outer chain + spokes into the trusted ring
  [8, 9, 'pool'],
  [9, 10, 'pool'],
  [10, 11, 'pool'],
  [11, 12, 'pool'],
  [12, 13, 'pool'],
  [13, 14, 'pool'],
  [14, 15, 'pool'],
  [15, 16, 'pool'],
  [16, 17, 'pool'],
  [17, 18, 'pool'],
  [18, 19, 'pool'],
  [19, 20, 'pool'],
  [20, 8, 'pool'],
  [8, 21, 'pool'],
  [21, 9, 'pool'],
  [9, 3, 'pool'],
  [10, 5, 'pool'],
  [12, 5, 'pool'],
  [13, 6, 'pool'],
  [15, 6, 'pool'],
  [17, 4, 'pool'],
  [19, 4, 'pool'],
  [20, 7, 'pool'],
  [12, 2, 'pool'],
];

// Which groups a scope activates, and how brightly.
const SCOPE_ALPHA: Record<MeshScope, Record<Group, number>> = {
  local: { yours: 1, trusted: 0.22, pool: 0.1 },
  trusted: { yours: 1, trusted: 1, pool: 0.14 },
  pool: { yours: 1, trusted: 1, pool: 1 },
};

// Max random-walk hops per scope. Local requests stay home.
const SCOPE_HOPS: Record<MeshScope, number> = { local: 2, trusted: 3, pool: 5 };

const GROUP_COLOR: Record<Group, { core: string; glow: string }> = {
  yours: { core: '232, 148, 106', glow: '224, 136, 86' },
  trusted: { core: '134, 174, 148', glow: '127, 169, 140' },
  pool: { core: '167, 154, 140', glow: '150, 138, 124' },
};

const LINE_RGB = '216, 206, 192';
const PULSE_RGB = '236, 158, 112';
const MAX_PULSES = 6;

// ─── Component ────────────────────────────────────────────────────────────────

export function MeshCanvas({ scope, className }: { scope: MeshScope; className?: string }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const scopeRef = useRef<MeshScope>(scope);
  // Bumped on scope change so the loop can spawn a visible reroute burst.
  const scopeEpochRef = useRef(0);
  // Assigned by the canvas effect; lets the scope effect redraw static frames.
  const staticRedrawRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (scopeRef.current !== scope) {
      scopeRef.current = scope;
      scopeEpochRef.current += 1;
    }
  }, [scope]);

  useEffect(() => {
    // Re-bind after the null guards: hoisted inner functions type-check
    // against declared types, so `canvas`/`ctx` must be declared non-nullable.
    const maybeCanvas = canvasRef.current;
    if (!maybeCanvas) return;
    const canvas = maybeCanvas;
    const maybeCtx = canvas.getContext('2d');
    if (!maybeCtx) return;
    const ctx = maybeCtx;

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // Mutable simulation state, local to this effect.
    const nodes: MeshNode[] = NODES.map((n, i) => ({ ...n, phase: (i * 2.399) % (Math.PI * 2) }));
    const edges: MeshEdge[] = EDGES.map(([a, b, group]) => ({ a, b, group, heat: 0 }));
    const groupAlpha: Record<Group, number> = {
      yours: 1,
      trusted: SCOPE_ALPHA[scopeRef.current].trusted,
      pool: SCOPE_ALPHA[scopeRef.current].pool,
    };
    let pulses: Pulse[] = [];
    let lastSpawn = 0;
    let seenEpoch = scopeEpochRef.current;
    let width = 0;
    let height = 0;
    let raf = 0;
    let running = false;
    let visible = true;
    let lastTs = 0;

    // Adjacency over edges whose group is active enough to carry traffic.
    function activeAdjacency(scopeNow: MeshScope): Map<number, number[]> {
      const allowed: Record<Group, boolean> = {
        yours: true,
        trusted: scopeNow !== 'local',
        pool: scopeNow === 'pool',
      };
      const adj = new Map<number, number[]>();
      const link = (from: number, to: number): void => {
        const list = adj.get(from);
        if (list) list.push(to);
        else adj.set(from, [to]);
      };
      for (const e of edges) {
        if (!allowed[e.group]) continue;
        link(e.a, e.b);
        link(e.b, e.a);
      }
      return adj;
    }

    function findEdge(a: number, b: number): MeshEdge | undefined {
      return edges.find((e) => (e.a === a && e.b === b) || (e.a === b && e.b === a));
    }

    // A request starts on one of your machines and random-walks outward.
    function spawnPulse(scopeNow: MeshScope): void {
      const adj = activeAdjacency(scopeNow);
      const start = Math.floor(Math.random() * 3); // yours: indices 0-2
      const maxHops = 1 + Math.floor(Math.random() * SCOPE_HOPS[scopeNow]);
      const path = [start];
      let current = start;
      let prev = -1;
      for (let hop = 0; hop < maxHops; hop++) {
        const neighbors = (adj.get(current) ?? []).filter((n) => n !== prev);
        if (neighbors.length === 0) break;
        const next = neighbors[Math.floor(Math.random() * neighbors.length)];
        if (next === undefined) break;
        path.push(next);
        prev = current;
        current = next;
      }
      if (path.length < 2) return;
      pulses.push({ path, seg: 0, t: 0, speed: 0.9 + Math.random() * 0.5 });
    }

    function nodeXY(n: MeshNode, t: number): { x: number; y: number } {
      // Mesh sits right-of-center on wide screens so hero copy owns the left.
      const wide = width / Math.max(height, 1) > 0.9;
      const cx = width * (wide ? 0.62 : 0.5);
      const cy = height * (wide ? 0.5 : 0.42);
      const unit = Math.min(width, height) * (wide ? 0.44 : 0.52);
      // Gentle idle drift, ~3px, unique per node.
      const dx = reducedMotion ? 0 : Math.sin(t * 0.00045 + n.phase) * 3;
      const dy = reducedMotion ? 0 : Math.cos(t * 0.00038 + n.phase * 1.7) * 3;
      return { x: cx + n.ux * unit + dx, y: cy + n.uy * unit + dy };
    }

    function draw(ts: number): void {
      const dt = lastTs === 0 ? 16 : Math.min(ts - lastTs, 48);
      lastTs = ts;
      const scopeNow = scopeRef.current;

      // Ease group brightness toward the scope target.
      const target = SCOPE_ALPHA[scopeNow];
      for (const g of ['yours', 'trusted', 'pool'] as const) {
        groupAlpha[g] += (target[g] - groupAlpha[g]) * Math.min((dt / 16) * 0.07, 1);
      }

      // On scope change, retire pulses that are no longer routable and burst.
      if (seenEpoch !== scopeEpochRef.current) {
        seenEpoch = scopeEpochRef.current;
        pulses = [];
        for (let i = 0; i < 3; i++) spawnPulse(scopeNow);
        lastSpawn = ts;
      }

      if (!reducedMotion && ts - lastSpawn > 700 + Math.random() * 400 && pulses.length < MAX_PULSES) {
        spawnPulse(scopeNow);
        lastSpawn = ts;
      }

      ctx.clearRect(0, 0, width, height);

      // Edges.
      ctx.lineWidth = 1;
      for (const e of edges) {
        const na = nodes[e.a];
        const nb = nodes[e.b];
        if (!na || !nb) continue;
        const alpha = Math.min(groupAlpha[na.group], groupAlpha[nb.group]);
        const pa = nodeXY(na, ts);
        const pb = nodeXY(nb, ts);
        ctx.strokeStyle = `rgba(${LINE_RGB}, ${(0.05 + alpha * 0.1).toFixed(3)})`;
        ctx.beginPath();
        ctx.moveTo(pa.x, pa.y);
        ctx.lineTo(pb.x, pb.y);
        ctx.stroke();
        if (e.heat > 0.01) {
          ctx.strokeStyle = `rgba(${PULSE_RGB}, ${(e.heat * 0.35).toFixed(3)})`;
          ctx.beginPath();
          ctx.moveTo(pa.x, pa.y);
          ctx.lineTo(pb.x, pb.y);
          ctx.stroke();
          e.heat *= Math.pow(0.92, dt / 16);
        }
      }

      // Pulses.
      if (!reducedMotion) {
        const alive: Pulse[] = [];
        for (const p of pulses) {
          p.t += (p.speed * dt) / 1000;
          while (p.t >= 1 && p.seg < p.path.length - 2) {
            p.t -= 1;
            p.seg += 1;
          }
          if (p.t >= 1) continue; // finished its last segment
          const ia = p.path[p.seg];
          const ib = p.path[p.seg + 1];
          if (ia === undefined || ib === undefined) continue;
          const na = nodes[ia];
          const nb = nodes[ib];
          if (!na || !nb) continue;
          const edge = findEdge(ia, ib);
          if (edge) edge.heat = 1;
          const pa = nodeXY(na, ts);
          const pb = nodeXY(nb, ts);
          const x = pa.x + (pb.x - pa.x) * p.t;
          const y = pa.y + (pb.y - pa.y) * p.t;
          const glow = ctx.createRadialGradient(x, y, 0, x, y, 14);
          glow.addColorStop(0, `rgba(${PULSE_RGB}, 0.85)`);
          glow.addColorStop(0.35, `rgba(${PULSE_RGB}, 0.25)`);
          glow.addColorStop(1, `rgba(${PULSE_RGB}, 0)`);
          ctx.fillStyle = glow;
          ctx.beginPath();
          ctx.arc(x, y, 14, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = `rgba(255, 240, 228, 0.95)`;
          ctx.beginPath();
          ctx.arc(x, y, 2, 0, Math.PI * 2);
          ctx.fill();
          alive.push(p);
        }
        pulses = alive;
      }

      // Nodes + labels. Canvas fonts need concrete family names, not CSS vars.
      ctx.font = '11px "Spline Sans Mono", ui-monospace, monospace';
      for (const n of nodes) {
        const alpha = groupAlpha[n.group];
        if (alpha < 0.02) continue;
        const { x, y } = nodeXY(n, ts);
        const breathe = reducedMotion ? 1 : 1 + 0.08 * Math.sin(ts * 0.0008 + n.phase);
        const r = n.r * breathe;
        const color = GROUP_COLOR[n.group];
        const glow = ctx.createRadialGradient(x, y, 0, x, y, r * 4);
        glow.addColorStop(0, `rgba(${color.glow}, ${(alpha * 0.35).toFixed(3)})`);
        glow.addColorStop(1, `rgba(${color.glow}, 0)`);
        ctx.fillStyle = glow;
        ctx.beginPath();
        ctx.arc(x, y, r * 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = `rgba(${color.core}, ${Math.min(alpha + 0.1, 1).toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
        if (n.label) {
          ctx.fillStyle = `rgba(${LINE_RGB}, ${(alpha * 0.55).toFixed(3)})`;
          ctx.fillText(n.label, x + r + 8, y + 3.5);
        }
      }
    }

    function frame(ts: number): void {
      draw(ts);
      if (running && !reducedMotion) raf = requestAnimationFrame(frame);
    }

    function start(): void {
      if (running || !visible || document.hidden) return;
      running = true;
      lastTs = 0;
      raf = requestAnimationFrame(frame);
    }

    function stop(): void {
      running = false;
      cancelAnimationFrame(raf);
    }

    function resize(): void {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = rect.width;
      height = rect.height;
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (reducedMotion) draw(performance.now());
    }

    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    const io = new IntersectionObserver(
      (entries) => {
        visible = entries[0]?.isIntersecting ?? true;
        if (visible) start();
        else stop();
      },
      { threshold: 0.02 }
    );
    io.observe(canvas);

    function onVisibility(): void {
      if (document.hidden) stop();
      else start();
    }
    document.addEventListener('visibilitychange', onVisibility);

    function drawStaticFrame(): void {
      // Snap brightness to the scope target so a single frame reads correctly.
      groupAlpha.yours = SCOPE_ALPHA[scopeRef.current].yours;
      groupAlpha.trusted = SCOPE_ALPHA[scopeRef.current].trusted;
      groupAlpha.pool = SCOPE_ALPHA[scopeRef.current].pool;
      draw(performance.now());
    }

    if (reducedMotion) {
      staticRedrawRef.current = drawStaticFrame;
      drawStaticFrame();
    } else {
      start();
    }

    return () => {
      stop();
      staticRedrawRef.current = null;
      ro.disconnect();
      io.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // scope changes flow through scopeRef; the sim must not restart on toggle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reduced-motion static frames still need to reflect the chosen scope.
  useEffect(() => {
    staticRedrawRef.current?.();
  }, [scope]);

  return (
    <canvas
      ref={canvasRef}
      className={className}
      role="img"
      aria-label="A live map of the Potluck mesh: your machines in ember, the trusted circle in sage, the open pool beyond. Requests travel between whichever machines the selected scope allows."
    />
  );
}
