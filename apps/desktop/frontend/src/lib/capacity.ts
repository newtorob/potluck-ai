/**
 * Household capacity — "what can your network actually run, right now?"
 *
 * Takes the live peer roster + their advertised capabilities (`/peer/capabilities`)
 * and bins each model-size class into a speed bucket: fast / good / slow /
 * unavailable. This is what powers the CapacityCard on the Household view.
 *
 * Honesty rule (memory 20fd8d9f): the buckets here are computed from a
 * conservative static table. They MUST NOT pretend a peer can do something
 * it can't (e.g. running a 70B model on an 11GB GPU). When in doubt,
 * downgrade. The user trusting "8B good" and then watching it stall at
 * 0.3 tok/s erodes trust faster than honest "unavailable."
 *
 * Privacy: rendering the buckets only requires aggregate counts. We never
 * publish per-peer numbers in this card; click-through to the peer roster
 * is the path for "which machine does what."
 */
import type { MeshPeer } from '@/types/mesh';
import {
  probePeer,
  getBigModelEligiblePeers,
  type PeerCapabilities,
  type PeerHardware,
} from '@/lib/householdInference';

// ---------------------------------------------------------------------------
// Buckets
// ---------------------------------------------------------------------------

/** Coarse model-size buckets in billions of parameters. The boundaries match
 *  what the model registry already uses for `recommended_tier`. */
export type SizeBucket = 'small' | 'medium' | 'large' | 'xl' | 'xxl';

/** Speed buckets for a given (hardware tier, model size) combination.
 *  Numbers are tok/s ranges, but we render the categorical label only. */
export type SpeedBucket = 'fast' | 'good' | 'slow' | 'unavailable';

export interface SizeBucketSpec {
  bucket: SizeBucket;
  /** Human label for the UI ("3B", "8B", "32B", "70B", ">70B"). */
  label: string;
  /** Approximate parameter count in billions — used for VRAM math. */
  paramsB: number;
}

export const SIZE_BUCKETS: SizeBucketSpec[] = [
  { bucket: 'small', label: '3B', paramsB: 3 },
  { bucket: 'medium', label: '8B', paramsB: 8 },
  { bucket: 'large', label: '32B', paramsB: 32 },
  { bucket: 'xl', label: '70B', paramsB: 70 },
];
// Note: 'xxl' (>70B) is intentionally omitted from the default render —
// almost no consumer hardware can run those even with multi-peer pooling,
// and showing it would be honesty-violating noise. Add back when there's
// a peer in the wild that can actually serve a 100B+ model.

// ---------------------------------------------------------------------------
// VRAM math
// ---------------------------------------------------------------------------

// Rough VRAM cost in GB for Q4-Q5 quantized weights.
// 4-bit quant ≈ 0.5 GB per billion parameters; plus ~25% for KV cache + overhead.
// This is intentionally a coarse heuristic — we'd rather flag a peer as
// "can't run" and be wrong by a small margin than the other way around.
function approxVRAMForModel(paramsB: number): number {
  return paramsB * 0.5 * 1.25;
}

/** A peer can either fit a model fully in GPU VRAM (fast path) or partly /
 *  fully on CPU+RAM (slow path). Returns 'gpu' or 'cpu' for the fit mode,
 *  or 'none' when neither works. Callers downgrade speed by one bucket
 *  when mode === 'cpu' to reflect the offload penalty honestly. */
type FitMode = 'gpu' | 'cpu' | 'none';

function peerFitMode(caps: PeerCapabilities, paramsB: number): FitMode {
  const needed = approxVRAMForModel(paramsB);
  const vram = caps.hardware?.gpu_vram_gb;
  if (vram != null && vram >= needed) {
    return 'gpu';
  }
  // Fall back to system RAM with a 1.5x headroom for KV cache + activations.
  // This is the path llama.cpp takes when -ngl < total layers: hot tensors
  // stay in GPU, cold tensors stream from RAM. Throughput drops sharply
  // (typically ~5-10x slower than full-GPU), which the caller reflects by
  // downgrading the speed bucket.
  const ram = caps.hardware?.ram_total_gb ?? 0;
  if (ram >= needed * 1.5) {
    return 'cpu';
  }
  return 'none';
}

// ---------------------------------------------------------------------------
// Speed table
// ---------------------------------------------------------------------------

// Lookup: (hardware tier, model-size bucket) → expected tok/s bucket.
// Numbers represent reasonable Q4-Q5 throughput on typical hardware in each
// tier. They're conservative — most users will see at-or-above these speeds.
//
// Tiers come from /peer/capabilities → hardware.recommended_tier, which the
// hardware-detect service computes from (GPU model, VRAM, CPU cores, RAM).
//   low    = no dedicated GPU OR <8GB VRAM (CPU-bound)
//   medium = 8-16GB VRAM (RTX 2080 Ti, M2/M3 base, etc.)
//   high   = 16GB+ VRAM (RTX 3090/4090, M3 Max, M2 Ultra)
//
// TODO(c2e13a86): replace with real benchmark data once bench/ is wired
// (paired with task 7aaac77f). For now: conservative-but-not-fake estimates.
const SPEED_TABLE: Record<string, Record<SizeBucket, SpeedBucket>> = {
  low: {
    small: 'good',
    medium: 'slow',
    large: 'unavailable',
    xl: 'unavailable',
    xxl: 'unavailable',
  },
  medium: {
    small: 'fast',
    medium: 'fast',
    large: 'slow',
    xl: 'unavailable',
    xxl: 'unavailable',
  },
  high: {
    small: 'fast',
    medium: 'fast',
    large: 'good',
    xl: 'slow',
    xxl: 'unavailable',
  },
};

function speedFor(tier: string | null | undefined, bucket: SizeBucket): SpeedBucket {
  const t = (tier ?? 'low').toLowerCase();
  return SPEED_TABLE[t]?.[bucket] ?? 'unavailable';
}

// ---------------------------------------------------------------------------
// Bucket aggregation
// ---------------------------------------------------------------------------

/** Ordinal rank of each speed bucket, so the higher (better) speed wins when
 *  comparing peers — dispatch picks the best available peer for a request. */
const SPEED_RANK: Record<SpeedBucket, number> = {
  unavailable: 0,
  slow: 1,
  good: 2,
  fast: 3,
};

/** Drop one rung on the speed ladder. Used when a peer is forced to
 *  CPU/RAM-offload because the model doesn't fit in VRAM. */
function downgradeSpeed(s: SpeedBucket): SpeedBucket {
  switch (s) {
    case 'fast': return 'good';
    case 'good': return 'slow';
    case 'slow': return 'slow'; // already at the floor
    case 'unavailable': return 'unavailable';
  }
}

export interface CapacityResult {
  bucket: SizeBucket;
  label: string;
  paramsB: number;
  speed: SpeedBucket;
  /** Best peer hostname for this bucket — used only in click-through. NOT
   *  rendered on the card. Null if no peer can run this size. */
  bestPeerHost: string | null;
  /** Big Model Mode pooling could unlock this bucket. Surfaced in the UI as
   *  a "with multiple machines: …" hint for xl/xxl. */
  bigModelEligible: boolean;
}

export interface CapacitySnapshot {
  buckets: CapacityResult[];
  /** Total reachable peers contributing capacity. */
  peerCount: number;
  /** True if at least one peer is offering rpc compute (Big Model Mode). */
  bigModelPoolSize: number;
}

/** Probe every reachable peer once and compute the capacity snapshot.
 *  Cheap — one GET /peer/capabilities per peer in parallel. */
export async function computeCapacity(peers: MeshPeer[]): Promise<CapacitySnapshot> {
  const reachable = peers.filter((p) => p.reachable);
  if (reachable.length === 0) {
    return {
      buckets: SIZE_BUCKETS.map((s) => ({
        bucket: s.bucket,
        label: s.label,
        paramsB: s.paramsB,
        speed: 'unavailable' as SpeedBucket,
        bestPeerHost: null,
        bigModelEligible: false,
      })),
      peerCount: 0,
      bigModelPoolSize: 0,
    };
  }

  const probes = await Promise.all(reachable.map((p) => probePeer(p)));
  const probePairs: { peer: MeshPeer; caps: PeerCapabilities }[] = [];
  for (let i = 0; i < reachable.length; i++) {
    const caps = probes[i];
    const peer = reachable[i];
    if (peer && caps) {
      probePairs.push({ peer, caps });
    }
  }

  // Big Model Mode eligibility — separate query because it has additional
  // gating (rpc_endpoint + available_for_big_model). The pool size informs
  // the xl/xxl "with pooling" hint.
  const bigModelPeers = await getBigModelEligiblePeers(reachable);
  const bigModelPoolSize = bigModelPeers.length;

  const buckets: CapacityResult[] = SIZE_BUCKETS.map((spec) => {
    let best: SpeedBucket = 'unavailable';
    let bestHost: string | null = null;
    for (const { peer, caps } of probePairs) {
      const mode = peerFitMode(caps, spec.paramsB);
      if (mode === 'none') continue;
      let speed = speedFor(caps.hardware?.recommended_tier, spec.bucket);
      // CPU/offload path: downgrade by one bucket. The hardware tier table
      // assumes the model fits in GPU; once we start streaming from RAM,
      // throughput drops ~5-10x. Honest framing > optimistic UX.
      if (mode === 'cpu' && speed !== 'unavailable') {
        speed = downgradeSpeed(speed);
      }
      if (SPEED_RANK[speed] > SPEED_RANK[best]) {
        best = speed;
        bestHost = caps.hostname || peer.node_name;
      }
    }

    // Big Model pooling: an xl/xxl model that no single peer can fit might
    // be unlockable by pooling. Honesty-rule: "slow" only — never claim
    // pooled inference is fast. The capacity-not-speed framing is the
    // honest one (memory 20fd8d9f).
    let bigModelEligible = false;
    if (best === 'unavailable' && bigModelPoolSize >= 2 && (spec.bucket === 'xl' || spec.bucket === 'xxl')) {
      bigModelEligible = true;
      best = 'slow';
    }

    return {
      bucket: spec.bucket,
      label: spec.label,
      paramsB: spec.paramsB,
      speed: best,
      bestPeerHost: bestHost,
      bigModelEligible,
    };
  });

  return {
    buckets,
    peerCount: probePairs.length,
    bigModelPoolSize,
  };
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

export function speedLabel(speed: SpeedBucket): string {
  switch (speed) {
    case 'fast': return 'fast';
    case 'good': return 'good';
    case 'slow': return 'slow';
    case 'unavailable': return 'unavailable';
  }
}

export function speedHint(speed: SpeedBucket): string {
  switch (speed) {
    case 'fast': return '> 20 tok/s';
    case 'good': return '8 - 20 tok/s';
    case 'slow': return '1 - 8 tok/s';
    case 'unavailable': return 'no peer can run this';
  }
}

/** Tailwind classes for the bucket pill. Color-coded by speed but with a
 *  muted palette so the card doesn't feel like a status board. */
export function speedColorClasses(speed: SpeedBucket): string {
  switch (speed) {
    case 'fast': return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300';
    case 'good': return 'border-sky-500/30 bg-sky-500/10 text-sky-300';
    case 'slow': return 'border-amber-500/30 bg-amber-500/10 text-amber-300';
    case 'unavailable': return 'border-border/40 bg-muted/30 text-muted-foreground';
  }
}

/** Unused but kept for potential expanded view — what hardware the user
 *  would need to unlock the next bucket. */
export function upgradeHint(speed: SpeedBucket, bucket: SizeBucket): string | null {
  if (speed !== 'unavailable') return null;
  switch (bucket) {
    case 'large': return 'add a peer with 16GB+ VRAM';
    case 'xl': return 'add a peer with 24GB+ VRAM, or two peers for Big Model Mode';
    case 'xxl': return 'multiple peers with Big Model Mode';
    default: return null;
  }
}

// Re-export PeerHardware for components that want to render per-peer detail.
export type { PeerHardware };
