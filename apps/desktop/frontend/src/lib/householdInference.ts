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

const CAPABILITIES_TIMEOUT_MS = 1_500;

/** Probe a single household peer for its current capabilities. Returns
 *  null on any failure (unreachable, non-2xx, malformed body) — null
 *  is just "not available," never an exception. */
export async function probePeer(peer: MeshPeer): Promise<PeerCapabilities | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CAPABILITIES_TIMEOUT_MS);
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

  const probes = await Promise.all(reachable.map((p) => probePeer(p)));

  // Pair peers with their probe results, dropping any that failed to
  // respond or aren't available_for_inference. After this point we
  // know every entry is a viable target.
  const eligible: { peer: MeshPeer; capabilities: PeerCapabilities }[] = [];
  for (let i = 0; i < reachable.length; i++) {
    const caps = probes[i];
    const peer = reachable[i];
    if (peer && caps && caps.available_for_inference) {
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
}

export class HouseholdNoPeerError extends Error {
  readonly kind = 'no_peer' as const;
  constructor() {
    super('No reachable household peer is currently in contributor mode with a model loaded.');
    this.name = 'HouseholdNoPeerError';
  }
}

/**
 * Dispatch chat messages to the first available household peer.
 *
 * Returns the raw fetch Response (SSE stream). Caller pipes it through
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
  const lastUserContent = messages
    .filter((m) => m.role === 'user')
    .pop()?.content ?? '';
  const pick = await pickPeerForPrompt(peers, {
    promptCharLength: lastUserContent.length,
    mode: options.routingMode ?? 'capability',
  });
  if (pick === null) {
    throw new HouseholdNoPeerError();
  }

  const { peer, capabilities } = pick;

  const apiMessages = messages.map((m) => ({
    role: m.role,
    content: m.content,
  }));

  const init: RequestInit = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      messages: apiMessages,
      stream: true,
      temperature: options.temperature ?? 0.7,
      max_tokens: options.maxTokens ?? 2048,
    }),
  };
  if (options.abortSignal) init.signal = options.abortSignal;
  const response = await fetch(`${peerBaseURL(peer)}/peer/chat/completions`, init);

  return { response, peer, capabilities };
}
