/**
 * Household inference — dispatch chat requests to a peer's Python sidecar
 * over the WireGuard household mesh.
 *
 * This is the third inference path alongside the existing two:
 *
 *   1. **Local** — `localApi.streamChat()` posts to 127.0.0.1:8321 (this
 *      machine's Python sidecar with full memory + history injection).
 *   2. **Cooperative network** — `localApi.streamNetworkJob()` posts to
 *      the coordinator's `/jobs/stream`, which fans out to a contributor
 *      peer via blinded routing. Member identity is hidden by design.
 *   3. **Household** (this module) — directly POSTs to a household
 *      peer's `/peer/chat/completions` over the mesh IP. Goes through
 *      no coordinator and no third-party routing. The trust model is
 *      "I paired this machine into my household; I trust it."
 *
 * Why this exists:
 *   The user has a beefier media server on their LAN. They want to run
 *   inference on it from their laptop without (a) loading models locally,
 *   (b) routing through the cooperative network (which is for serving
 *   strangers, not your own boxes), or (c) wiring up Tailscale and
 *   coordinator-bound-to-LAN by hand.
 *
 * Why this is private:
 *   The peer endpoint (`/peer/chat/completions` in the Python sidecar)
 *   is STATELESS — no memory injection, no conversation persistence, no
 *   observability session. The serving peer remembers nothing about the
 *   request. The coordinator never sees the prompt.
 *
 * Wire protocol:
 *   Identical to the local `/chat/completions` SSE stream — same
 *   OpenAI-format `data: {...}` chunks — so `streamTokens()` in
 *   lib/streaming.ts parses both shapes with no changes.
 */

import type { MeshPeer } from '@/types/mesh';
import type { ChatMessage } from '@/types/chat';
import { streamTokens } from '@/lib/streaming';
import type { StreamEvent } from '@/lib/streaming';

const PEER_SIDECAR_PORT = 8321;

// Reach a peer's Python sidecar through its mesh IP. Browsers can dial
// 100.64.0.x normally; the WireGuard interface routes those packets
// through the userspace tunnel transparently.
function peerBaseURL(peer: MeshPeer): string {
  return `http://${peer.mesh_ip}:${PEER_SIDECAR_PORT}`;
}

// ---------------------------------------------------------------------------
// Capabilities probe
// ---------------------------------------------------------------------------

export interface PeerHardware {
  platform: string | null;
  arch: string | null;
  cpu_cores_physical: number | null;
  cpu_cores_logical: number | null;
  ram_total_gb: number | null;
  gpu_name: string | null;
  gpu_vram_gb: number | null;
  recommended_tier: string | null; // 'low' | 'medium' | 'high'
}

export interface PeerLoadedModel {
  slug: string;
  context_size: number | null;
  tokens_per_sec?: number | null;
}

export interface PeerInferenceLoad {
  busy: boolean;
  recent_completions: number | null;
}

export interface PeerCapabilities {
  hostname: string;
  loaded_model: string | null;
  contributor_mode: boolean;
  available_for_inference: boolean;
  protocol_version: number;
  // Additive fields (protocol_version >= 2). Older peers will return
  // these as undefined; consumers must handle that gracefully.
  hardware?: PeerHardware | null;
  loaded_model_detail?: PeerLoadedModel | null;
  inference_load?: PeerInferenceLoad | null;
  // Big Model Mode (protocol_version >= 3). rpc_endpoint is where this
  // peer's llama.cpp rpc-server listens; the requesting orchestrator
  // includes it in its own /models/<slug>/load when enabling Big Model
  // Mode. available_for_big_model is true when rpc_endpoint is set AND
  // contributor mode is on.
  rpc_endpoint?: string | null;
  available_for_big_model?: boolean;
  // Pipeline-parallel (protocol_version >= 4). True when this peer can serve a
  // pipeline stage: torch present, contributor mode on, AND POTLUCK_PIPELINE_ENABLED
  // set. The head sidecar does the actual orchestration + stage starts; the
  // frontend only needs this flag to decide whether to offer the toggle.
  available_for_pipeline?: boolean;
  // Network link type (protocol_version >= 5). Populated from the peer's
  // interface probe; null / undefined means the peer hasn't reported it yet.
  // WiFi peers add latency and jitter in pipeline-parallel inference because
  // activations cross the wire on every token — wiring them (Ethernet) is the
  // single biggest real-world speedup the user can make.
  link_type?: 'wired' | 'wifi' | 'unknown' | null;
}

/** Routing mode for picking which household peer serves a request. */
export type RoutingMode = 'capability' | 'round_robin';

// ---------------------------------------------------------------------------
// Big Model Mode helpers
// ---------------------------------------------------------------------------

/** Probe all reachable household peers and return the subset that's
 *  advertising an rpc_endpoint AND has contributor mode on. These are
 *  the peers eligible to contribute compute when the local sidecar
 *  loads a model with rpc_servers (Big Model Mode). */
export async function getBigModelEligiblePeers(
  peers: MeshPeer[],
): Promise<{ peer: MeshPeer; capabilities: PeerCapabilities }[]> {
  const reachable = peers.filter((p) => p.reachable);
  if (reachable.length === 0) return [];
  const probes = await Promise.all(reachable.map((p) => probePeer(p)));
  const out: { peer: MeshPeer; capabilities: PeerCapabilities }[] = [];
  for (let i = 0; i < reachable.length; i++) {
    const caps = probes[i];
    const peer = reachable[i];
    if (peer && caps && caps.available_for_big_model && caps.rpc_endpoint) {
      out.push({ peer, capabilities: caps });
    }
  }
  return out;
}

/** Probe all reachable household peers and return the subset eligible to serve a
 *  pipeline-parallel stage (available_for_pipeline). Unlike Big Model Mode, the
 *  head sidecar does the actual orchestration + stage starts — this is only used
 *  by the UI to decide whether to offer the toggle and to show how many peers
 *  could participate. */
export async function getPipelineEligiblePeers(
  peers: MeshPeer[],
): Promise<{ peer: MeshPeer; capabilities: PeerCapabilities }[]> {
  const reachable = peers.filter((p) => p.reachable);
  if (reachable.length === 0) return [];
  const probes = await Promise.all(reachable.map((p) => probePeer(p)));
  const out: { peer: MeshPeer; capabilities: PeerCapabilities }[] = [];
  for (let i = 0; i < reachable.length; i++) {
    const caps = probes[i];
    const peer = reachable[i];
    if (peer && caps && caps.available_for_pipeline) {
      out.push({ peer, capabilities: caps });
    }
  }
  return out;
}

const CAPABILITIES_TIMEOUT_MS = 1_500;

/** Probe a single household peer for its current capabilities. Returns
 *  null on any failure (unreachable, non-2xx, malformed body) — null
 *  is just "not available," never an exception. */
export async function probePeer(
  peer: MeshPeer, signal?: AbortSignal, timeoutMs = CAPABILITIES_TIMEOUT_MS,
): Promise<PeerCapabilities | null> {
  const controller = new AbortController();
  const onAbort = () => controller.abort(signal?.reason);
  signal?.addEventListener('abort', onAbort, { once: true });
  if (signal?.aborted) onAbort();
  const timer = setTimeout(() => controller.abort(), Math.max(0, timeoutMs));
  try {
    const res = await fetch(`${peerBaseURL(peer)}/peer/capabilities`, {
      method: 'GET',
      signal: controller.signal,
    });
    if (!res.ok) return null;
    const body = (await res.json()) as PeerCapabilities;
    return body;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }
}

/** Find a household peer that's reachable AND available for inference.
 *  Probes peers in parallel with a 1.5s timeout each. Returns the first
 *  successful match (in roster order) along with its capabilities.
 *
 *  This is the lowest-common-denominator picker — kept for legacy
 *  callers. New code should use {@link pickPeerForPrompt} which adds
 *  capability-aware scoring + round-robin support. */
export async function pickAvailablePeer(
  peers: MeshPeer[],
): Promise<{ peer: MeshPeer; capabilities: PeerCapabilities } | null> {
  const reachable = peers.filter((p) => p.reachable);
  if (reachable.length === 0) return null;

  const probes = await Promise.all(reachable.map((p) => probePeer(p)));
  for (let i = 0; i < reachable.length; i++) {
    const caps = probes[i];
    const peer = reachable[i];
    if (caps && peer && caps.available_for_inference) {
      return { peer, capabilities: caps };
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Capability-aware peer picker
// ---------------------------------------------------------------------------

/** Module-level cursor for round-robin mode. In-memory only; resets on
 *  reload. We don't persist this — coordinator-side state for routing
 *  decisions would be over-engineered for a UI knob, and "even spread
 *  this session" is a reasonable interpretation of the user's intent. */
let _roundRobinCursor = 0;

/** Score a peer for a given request. Higher = better choice. Negative
 *  means the peer is unusable. The scoring is intentionally simple — we
 *  want the routing decision to be legible, not a black box. */
function scorePeer(
  caps: PeerCapabilities,
  context: { promptCharLength: number },
): number {
  if (!caps.available_for_inference) return -1;

  let score = 0;

  // Base: any usable peer gets a baseline.
  score += 1;

  // Penalty: peer is currently serving another inference. Prefer idle
  // peers — single-machine inference doesn't pipeline.
  if (caps.inference_load?.busy) score -= 5;

  // Hardware bias: longer prompts benefit from more memory + faster GPUs.
  // The `recommended_tier` field is the cheapest signal — it's a coarse
  // bucket the hardware-detect service already computes. We treat it as
  // a soft preference, not a hard gate.
  const tier = caps.hardware?.recommended_tier;
  const isLongPrompt = context.promptCharLength > 800;
  if (isLongPrompt) {
    if (tier === 'high') score += 4;
    else if (tier === 'medium') score += 2;
  } else {
    // Short prompts: any tier is fine, but break ties toward higher-tier
    // peers (more headroom for unexpected long completions).
    if (tier === 'high') score += 1;
  }

  // Larger context windows are a real differentiator — favour peers
  // whose loaded model can hold long conversations.
  const ctx = caps.loaded_model_detail?.context_size ?? 0;
  if (ctx >= 32_768) score += 2;
  else if (ctx >= 8_192) score += 1;

  return score;
}

export interface PickPeerOptions {
  /** Prompt content used to inform hardware/context scoring. The full
   *  conversation isn't needed — just the latest user content is a
   *  good enough signal for "is this a long prompt?" */
  promptCharLength: number;
  mode: RoutingMode;
  modelSlug?: string;
  abortSignal?: AbortSignal;
  deadline?: number;
}

/** Pick the best household peer for this prompt, according to the
 *  chosen routing mode. Probes peers in parallel; returns null if none
 *  are available_for_inference.
 *
 *  - `capability` mode: scores each peer (hardware + load + model) and
 *    picks the highest. Ties broken by roster order.
 *  - `round_robin` mode: rotates a session-local cursor across all
 *    eligible peers — independent of hardware. Gives users an "even
 *    spread" option when they want to keep all their machines warm. */
export async function pickPeerForPrompt(
  peers: MeshPeer[],
  options: PickPeerOptions,
): Promise<{ peer: MeshPeer; capabilities: PeerCapabilities } | null> {
  const reachable = peers.filter((p) => p.reachable);
  if (reachable.length === 0) return null;

  const probeBudget = Math.min(CAPABILITIES_TIMEOUT_MS, (options.deadline ?? Infinity) - performance.now());
  const probes = await Promise.all(reachable.map((p) => probePeer(p, options.abortSignal, probeBudget)));

  // Pair peers with their probe results, dropping any that failed to
  // respond or aren't available_for_inference. After this point we
  // know every entry is a viable target.
  const eligible: { peer: MeshPeer; capabilities: PeerCapabilities }[] = [];
  for (let i = 0; i < reachable.length; i++) {
    const caps = probes[i];
    const peer = reachable[i];
    if (peer && caps && caps.available_for_inference && caps.loaded_model &&
        (!options.modelSlug || caps.loaded_model === options.modelSlug)) {
      eligible.push({ peer, capabilities: caps });
    }
  }
  if (eligible.length === 0) return null;

  if (options.mode === 'round_robin') {
    // Wrap around: cursor stays valid even if peers come and go.
    const idx = _roundRobinCursor % eligible.length;
    _roundRobinCursor = (idx + 1) % eligible.length;
    return eligible[idx] ?? null;
  }

  // Capability mode: score each, return the best. Stable on ties via
  // roster order (eligible[] preserves input ordering).
  let bestIdx = 0;
  let bestScore = scorePeer(eligible[0]!.capabilities, {
    promptCharLength: options.promptCharLength,
  });
  for (let i = 1; i < eligible.length; i++) {
    const score = scorePeer(eligible[i]!.capabilities, {
      promptCharLength: options.promptCharLength,
    });
    if (score > bestScore) {
      bestScore = score;
      bestIdx = i;
    }
  }
  return eligible[bestIdx] ?? null;
}

// ---------------------------------------------------------------------------
// Stream household inference
// ---------------------------------------------------------------------------

export interface HouseholdStreamOptions {
  temperature?: number;
  maxTokens?: number;
  abortSignal?: AbortSignal;
  routingMode?: RoutingMode;
  modelSlug?: string;
  excludedPeerIds?: readonly string[];
  getPeers?: () => MeshPeer[];
  onAttempt?: (peer: MeshPeer, model: string) => void;
}

export const HOUSEHOLD_RECOVERY = {
  maxAttempts: 3,
  firstTokenMs: 90_000,
  idleMs: 30_000,
  totalMs: 240_000,
};

function encodeEvent(event: StreamEvent): Uint8Array {
  const body = event.type === 'done' ? '[DONE]'
    : event.type === 'token' ? JSON.stringify({ choices: [{ delta: { content: event.content } }] })
      : event.type === 'meta' ? JSON.stringify({ meta: event })
        : JSON.stringify({ error: event });
  return new TextEncoder().encode(`data: ${body}\n\n`);
}

export class HouseholdNoPeerError extends Error {
  readonly kind = 'no_peer' as const;
  constructor() {
    super('No reachable household peer is currently in contributor mode with a model loaded.');
    this.name = 'HouseholdNoPeerError';
  }
}

/**
 * Find a household peer that actually produces output before committing.
 *
 * Returns a validated, bounded Response (SSE stream). Caller pipes it through
 * `streamTokens()` from lib/streaming.ts exactly like the local-sidecar
 * response — the wire format is identical.
 *
 * The conversation history IS sent (unlike the cooperative-network path,
 * which is single-turn for privacy reasons). The household trust model
 * permits it: the receiving peer is a machine you own.
 */
export async function streamHouseholdInference(
  messages: ChatMessage[],
  peers: MeshPeer[],
  options: HouseholdStreamOptions = {},
): Promise<{ response: Response; peer: MeshPeer; capabilities: PeerCapabilities }> {
  // Copy before the first await: edits/settings changes cannot rewrite a retry.
  const apiMessages = messages.map((m) => ({
    role: m.role,
    content: m.content,
  }));
  const deadline = performance.now() + HOUSEHOLD_RECOVERY.totalMs;
  const excluded = new Set(options.excludedPeerIds);
  let model = options.modelSlug;
  let lastError: unknown = new HouseholdNoPeerError();
  const promptChars = apiMessages.reduce((n, m) => n + m.content.length, 0);
  for (let attempt = 0; attempt < HOUSEHOLD_RECOVERY.maxAttempts; attempt++) {
    options.abortSignal?.throwIfAborted();
    if (performance.now() >= deadline) break;
    const pick = await pickPeerForPrompt(
      (options.getPeers?.() ?? peers).filter((p) => !excluded.has(p.node_id)),
      { promptCharLength: promptChars, mode: options.routingMode ?? 'capability',
        deadline, ...(options.abortSignal ? { abortSignal: options.abortSignal } : {}),
        ...(model ? { modelSlug: model } : {}) },
    );
    options.abortSignal?.throwIfAborted();
    if (performance.now() >= deadline) break;
    if (!pick) break;
    const { peer, capabilities } = pick;
    model ??= capabilities.loaded_model ?? undefined;
    if (!model) break;
    excluded.add(peer.node_id);
    options.onAttempt?.(peer, model);
    const controller = new AbortController();
    const onAbort = () => controller.abort(options.abortSignal?.reason);
    options.abortSignal?.addEventListener('abort', onAbort, { once: true });
    const rate = capabilities.loaded_model_detail?.tokens_per_sec;
    const firstMs = Math.min(HOUSEHOLD_RECOVERY.firstTokenMs,
      rate && rate > 0 ? 10_000 + (promptChars / 4 / rate) * 1000 : HOUSEHOLD_RECOVERY.firstTokenMs);
    const firstDeadline = Math.min(deadline, performance.now() + firstMs);
    const timer = setTimeout(() => controller.abort(new Error('The computer did not start the reply in time.')),
      Math.max(0, firstDeadline - performance.now()));
    let iterator: AsyncGenerator<StreamEvent> | undefined;
    const cleanup = () => {
      clearTimeout(timer);
      options.abortSignal?.removeEventListener('abort', onAbort);
      controller.abort();
    };
    try {
      const upstream = await fetch(`${peerBaseURL(peer)}/peer/chat/completions`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        body: JSON.stringify({ messages: apiMessages, model, stream: true,
          temperature: options.temperature ?? 0.7, max_tokens: options.maxTokens ?? 2048 }),
      });
      if (!upstream.ok) {
        await upstream.body?.cancel();
        const error = new Error(`That computer could not serve the request (HTTP ${upstream.status}).`);
        if (![408, 409, 429, 500, 502, 503, 504].includes(upstream.status)) {
          cleanup();
          throw Object.assign(error, { retryable: false });
        }
        throw error;
      }
      iterator = streamTokens(upstream, {
        signal: controller.signal, deadline, idleTimeoutMs: HOUSEHOLD_RECOVERY.idleMs,
        firstTokenTimeoutMs: Math.max(0, firstDeadline - performance.now()),
      });
      const first = await iterator.next();
      if (first.done) throw new Error('The connection ended before the reply started.');
      if (first.value.type === 'error') throw new Error(first.value.message);
      clearTimeout(timer);
      const events = iterator;
      let pending: StreamEvent | undefined = first.value;
      let finished = false;
      const response = new Response(new ReadableStream<Uint8Array>({
        async pull(out) {
          try {
            if (finished) { out.close(); return; }
            const next = pending ? { value: pending, done: false } : await events.next();
            pending = undefined;
            if (next.done) throw new Error('The connection ended before the reply finished.');
            out.enqueue(encodeEvent(next.value));
            if (next.value.type === 'done' || next.value.type === 'error') {
              finished = true;
              out.close();
              cleanup();
              await events.return(undefined);
            }
          } catch (error) {
            out.error(error);
            cleanup();
            await events.return(undefined);
          }
        },
        async cancel() {
          cleanup();
          await events.return(undefined);
        },
      }), { headers: { 'Content-Type': 'text/event-stream' } });
      return { response, peer, capabilities };
    } catch (error) {
      cleanup();
      await iterator?.return(undefined);
      options.abortSignal?.throwIfAborted();
      if (error instanceof Error && 'retryable' in error && error.retryable === false) throw error;
      lastError = error;
    }
  }
  throw lastError;
}
