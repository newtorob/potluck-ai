/**
 * HTTP client for the Python sidecar running at 127.0.0.1:8321.
 *
 * All inference and model management goes through this client.
 * Prompts never leave the machine - this API is strictly local.
 *
 * Base URL is configurable via VITE_LOCAL_API_URL for testing,
 * defaulting to the sidecar's hard-coded address.
 */

import type { ChatMessage, ChatOptions } from '@/types/chat';
import type { ModelAvailability, SplitModelPin, SplitSelection, SplitStatus } from '@/types/modelAvailability';
import type { Model, DownloadProgress } from '@/types/models';
import type {
  Memory,
  MemoryCategory,
  MemoryImportResult,
  MemoryWipeResult,
  MemoryExportResult,
  ProjectSummary,
  RelatedMemory,
  KeywordFrequency,
  ClusterResponse,
} from '@/types/memory';
import type { TaskState, TaskStatus, TaskCreateInput, TaskUpdateInput } from '@/types/tasks';
import type {
  AgentRun,
  AgentRunDetail,
  AgentRunEventDelta,
  AgentConnectorStatus,
  AgentProvider,
  AgentSession,
  ManagedAgentCapabilities,
  ManagedAgentSession,
  ManagedApprovalDecision,
  ManagedSandboxMode,
  MemoryPreview,
  SessionDetail,
  ToolCall,
} from '@/types/agents';
import { coordinatorBase } from '@/stores/connectionStore';
import {
  coordinatorSessionRequest,
  detailOf,
  type CoordinatorSessionResult,
} from '@/lib/coordinatorAuth';
import { normalizeCircle } from './circles';
import type { ContributorStatus } from './contributorStatus';

const LOCAL_BASE =
  (import.meta.env['VITE_LOCAL_API_URL'] as string | undefined) ?? 'http://127.0.0.1:8321';

// Coordinator URL - the Phase G control plane (separate from the local
// sidecar). Read from the runtime connection store (settings UI +
// localStorage) rather than a compile-time constant, so users can point
// at a coordinator on another machine in their fleet (LAN / Tailscale /
// hosted) without rebuilding. See `stores/connectionStore.ts` for the
// bootstrap precedence + persistence model. Imported below.

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => ({}));
    const msg =
      typeof body === 'object' && body !== null && 'detail' in body
        ? String(body.detail)
        : res.statusText;
    throw new Error(`[localApi] ${res.status}: ${msg}`);
  }
  return res.json() as Promise<T>;
}

/**
 * Like handleResponse but treats 204 No Content as success, returning void.
 * Used for DELETE endpoints that return no body.
 */
async function handle204(res: Response): Promise<void> {
  if (res.status === 204) return;
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => ({}));
    const msg =
      typeof body === 'object' && body !== null && 'detail' in body
        ? String(body.detail)
        : res.statusText;
    throw new Error(`[localApi] ${res.status}: ${msg}`);
  }
}

/**
 * Adapts a `coordinatorSessionRequest` result (which never throws) to the
 * same success/throw contract as `handleResponse`/`handle204`: any 2xx
 * status (including 204, for the circle/member delete routes, whose body is
 * unused) resolves the body cast to `T`; everything else throws
 * `[localApi] {status}: {detail}`, the shape coordinatorError.ts parses back
 * into a status and the coordinator's own message.
 */
function fromSessionResult<T>(res: CoordinatorSessionResult): T {
  if (res.status >= 200 && res.status < 300) {
    return res.body as T;
  }
  throw new Error(`[localApi] ${res.status}: ${detailOf(res.body, 'unknown error')}`);
}

// ---------------------------------------------------------------------------
// Timed fetch - bounds every non-streaming sidecar/coordinator call
// ---------------------------------------------------------------------------

// Captured before any rewrite so the wrapper always calls the real global.
const nativeFetch = fetch;

// Default ceiling for fast local calls (health, lists, CRUD, settings). Without
// it, a hung or still-starting sidecar leaves the promise pending forever, and a
// polling loop then stacks a backlog of requests that all resolve at once.
const DEFAULT_TIMEOUT_MS = 30_000;
// Model load streams a large GGUF into VRAM; extraction runs model inference.
// Give them generous ceilings so a legitimately slow op is not aborted.
const LOAD_TIMEOUT_MS = 180_000;
const EXTRACT_TIMEOUT_MS = 120_000;

/**
 * Wraps the native global with an automatic timeout. Streaming endpoints (SSE)
 * must not use this - they are long-lived and call nativeFetch() directly with
 * their own caller-provided abort signal. A signal passed in init is composed
 * with the timeout: whichever aborts first wins.
 */
async function timedFetch(
  input: string,
  init: RequestInit = {},
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<Response> {
  const ctrl = new AbortController();
  const incoming = init.signal ?? undefined;
  const onIncomingAbort = () => ctrl.abort(incoming?.reason);
  if (incoming) {
    if (incoming.aborted) ctrl.abort(incoming.reason);
    else incoming.addEventListener('abort', onIncomingAbort, { once: true });
  }
  const timer = setTimeout(
    () => ctrl.abort(new DOMException(`[localApi] timed out after ${timeoutMs}ms`, 'TimeoutError')),
    timeoutMs,
  );
  try {
    return await nativeFetch(input, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
    if (incoming) incoming.removeEventListener('abort', onIncomingAbort);
  }
}

// ---------------------------------------------------------------------------
// Network job streams (SSE: raw fetch, no timeout, the caller owns the abort)
// ---------------------------------------------------------------------------

/** POST /jobs/stream against the coordinator, as the renderer always has. */
function streamViaCoordinator(init: RequestInit): Promise<Response> {
  // coordinator-bare-fetch-ok: SSE stream, so it can't route through the
  // buffering coordinatorSessionRequest bridge (res.text() can't consume a
  // live stream). Authenticated at the network layer instead: main.js's
  // onBeforeSendHeaders injector (packaged app only) attaches the member
  // session bearer to requests matching the coordinator origin the
  // renderer pushes via 'coordinator:set-base'; see connectionStore.ts.
  return nativeFetch(`${coordinatorBase()}/jobs/stream`, init);
}

function discard(res: Response): void {
  void res.body?.cancel().catch(() => undefined);
}

/**
 * True when a 404 from /chat/circle came from an engine that has no such route
 * (FastAPI's bare {"detail":"Not Found"}, or no JSON detail at all), as opposed
 * to the coordinator's own answer relayed by the engine ("circle not found").
 * Reads a clone, so a response that is passed on still has its body.
 */
async function isMissingRoute(res: Response): Promise<boolean> {
  const body: unknown = await res.clone().json().catch(() => null);
  if (typeof body !== 'object' || body === null || !('detail' in body)) return true;
  const detail = body.detail;
  if (typeof detail === 'string') return detail === 'Not Found';
  // An object is the coordinator's structured answer; any other value is no answer.
  return !(typeof detail === 'object' && detail !== null);
}

/**
 * Ask Electron main to hand the member session to the engine again. Resolves
 * true when main had a session and the engine took it. The token stays in
 * main; the renderer only learns whether the hand-off happened. False outside
 * the Electron app, where there is no bridge.
 */
async function resyncEngineSession(): Promise<boolean> {
  if (typeof window === 'undefined') return false;
  const cloud = (
    window as unknown as { potluck?: { cloud?: { resyncEngineSession?: () => Promise<boolean> } } }
  ).potluck?.cloud;
  if (!cloud?.resyncEngineSession) return false;
  try {
    return (await cloud.resyncEngineSession()) === true;
  } catch {
    return false;
  }
}

/**
 * POST /chat/circle on the local engine. Resolves null when the engine cannot
 * take the request at all (no such route, or not running), so the caller can
 * use the coordinator instead. Any other answer, including a refusal, is
 * returned as the engine sent it.
 */
async function postCircleJobToEngine(init: RequestInit): Promise<Response | null> {
  let res: Response;
  try {
    res = await nativeFetch(`${LOCAL_BASE}/chat/circle`, init);
  } catch {
    // The caller stopping is not an engine that is down.
    init.signal?.throwIfAborted();
    return null;
  }
  if (res.status === 405 || (res.status === 404 && (await isMissingRoute(res)))) {
    discard(res);
    return null;
  }
  return res;
}

/**
 * A circle chat. The engine tries a direct link to a circle member's machine
 * and relays through the coordinator itself when it cannot, so it goes first.
 * The coordinator is the fallback only when the engine cannot take the request
 * (an engine from before /chat/circle, or none running) or answers 401 twice:
 * the engine holds the member session separately from the one the renderer's
 * own coordinator calls use, and it may not have it yet. The request body is
 * identical on every path and always says scope "circle"; nothing here sends a
 * circle chat to the open pool.
 */
async function streamCircleJob(init: RequestInit): Promise<Response> {
  let res = await postCircleJobToEngine(init);
  if (res?.status === 401) {
    discard(res);
    res = null;
    if (await resyncEngineSession()) {
      init.signal?.throwIfAborted();
      res = await postCircleJobToEngine(init);
      if (res?.status === 401) {
        discard(res);
        res = null;
      }
    }
  }
  if (res) return res;
  init.signal?.throwIfAborted();
  return streamViaCoordinator(init);
}

// ---------------------------------------------------------------------------
// Readiness probes
// ---------------------------------------------------------------------------

/**
 * Response from GET /readiness/gpu-test.
 *
 * Runs a real device op (matrix multiply or equivalent) on the selected
 * runtime. ok=true means the GPU/runtime actually computed - not just
 * "a GPU is detected". This is an action, not a passive poll: it may take
 * a moment and should never be called on a polling interval.
 */
export interface GpuTestResult {
  ok: boolean;
  device: 'cuda' | 'mps' | 'dml' | 'cpu';
  ran: 'inference' | 'compute' | 'none';
  latency_ms: number;
  detail: string;
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

/** Pipeline-parallel engine status (mirror of PipelineParallelEngine.describe()). */
export interface PipelineStatus {
  model_id: string;
  strategy: 'pipeline';
  head_layers: number;
  n_layers: number;
  downstream: string;
  device: string;
  dtype: string;
}

/** Whole-model engine status (mirror of WholeModelEngine.describe()). Surfaced on
 *  /health under loaded_pipeline when a model fits on this machine alone (the
 *  auto-pipeline planner's whole-model short-circuit); shares the same slot as
 *  PipelineStatus, discriminated by `strategy`. */
export interface WholeModelStatus {
  model_id: string;
  strategy: 'whole-model';
  n_layers: number;
  device: string;
  dtype: string;
}

export interface HealthResponse {
  status: 'ok';
  model_loaded: boolean;
  active_model_id: string | null;
  /** Present when the shared runtime owns startup restoration. */
  model_restore?: { status: 'idle' | 'disabled' | 'loading' | 'restored' | 'failed' | 'cancelled'; model_id: string | null };
  /** Context window actually loaded into the runtime. Null when no model is loaded. */
  n_ctx_loaded?: number | null;
  /** Big Model Mode: list of llama.cpp RPC endpoints the current model
   *  is distributed across. Null when loaded locally or no model loaded. */
  loaded_rpc_servers?: string[] | null;
  /** Pipeline-parallel: the distributed engine's status (head layers, the
   *  downstream peer, device/dtype) when a model is loaded with the pipeline
   *  strategy. Null otherwise. The other WAN-viable "big model" path,
   *  sibling to loaded_rpc_servers. Also carries the whole-model engine's
   *  status (strategy 'whole-model') when the model fits on this machine alone. */
  loaded_pipeline?: PipelineStatus | WholeModelStatus | null;
  /** Shared (continuous) batching: true when the loaded model is served through
   *  a continuous batcher so concurrent requests share batched forward passes.
   *  Only the transformers paths (whole-model / pipeline) can be loaded this way;
   *  always false on the single-stream + llama.cpp paths. The frontend reads this
   *  as the source of truth for the shared-batching toggle. */
  continuous_batch?: boolean;
}

// ---------------------------------------------------------------------------
// Settings shapes (mirror Python PotluckSettings)
// ---------------------------------------------------------------------------

export interface RuntimeSettings {
  startup_model_id?: string | null;
  context_size: number;
  gpu_layers: number;
  threads: number;
  temperature: number;
  max_tokens: number;
}

export interface PrivacySettings {
  chat_history_enabled: boolean;
  telemetry_enabled: boolean;
  crash_reports_enabled: boolean;
  memory_enabled: boolean;
}

export interface NetworkSettings {
  contributor_mode: boolean;
  /** Who else the machine answers while the toggle is on. Absent or null:
   *  not chosen, which keeps what the build did before the choice existed. */
  contributor_audience?: 'mine' | 'circles' | 'everyone' | null;
  /** Coordinator jobs this machine serves at once; the contributor loop
   *  declines beyond it and heartbeats carry it. */
  max_concurrent_jobs: number;
  contributor_schedule: Record<string, unknown> | null;
  max_gpu_percent: number;
  /** HTTP base of the coordinator this node talks to. Synced from the
   *  connection setting so the sidecar can reach the open pool. */
  coordinator_url: string;
}

export interface AppSettingsShape {
  theme: 'system' | 'light' | 'dark';
  font_size: number;
}

export interface MemorySettings {
  enabled: boolean;
  auto_extract: boolean;
  auto_retrieve: boolean;
  extraction_frequency: number;
  max_memories: number;
  max_retrieval: number;
  min_confidence_for_retrieval: number;
  retention_days: number;
  categories_enabled: MemoryCategory[];
}

export interface VaultSettings {
  folder: string | null;
  last_scan_at: string | null;
  file_count: number;
  chunk_count: number;
}

export interface GatewaySettings {
  enabled: boolean;
  api_key: string;
  /** Route gateway requests for models not held locally or in the household
   *  through the coordinator to the open pool. Opt-in, default off. */
  pool_routing: boolean;
}

export interface BenchmarkResult {
  model_id: string | null;
  output_tokens: number;
  ttft_ms: number;
  duration_ms: number;
  tokens_per_sec: number;
}

export type PotluckSettingsPatch = {
  [K in keyof PotluckSettings]?: PotluckSettings[K] extends object
    ? Partial<PotluckSettings[K]>
    : PotluckSettings[K];
};

export interface PotluckSettings {
  runtime: RuntimeSettings;
  privacy: PrivacySettings;
  network: NetworkSettings;
  app: AppSettingsShape;
  memory: MemorySettings;
  vault: VaultSettings;
  gateway: GatewaySettings;
  models_path: string;
  active_model_id: string | null;
  active_project: string | null;
  /** Generic string→string bag for frontend UI preferences (theme, routing
   *  scope, sidebar state, onboarding flag, etc.). The backend deep-merges
   *  patches so writing one key never drops another. */
  ui: Record<string, string>;
}

export interface VaultStatus {
  folder: string | null;
  last_scan_at: string | null;
  file_count: number;
  chunk_count: number;
}

export interface VaultRescanResult {
  files_scanned: number;
  files_added: number;
  files_updated: number;
  files_removed: number;
  chunks_inserted: number;
  chunks_deleted: number;
  file_count: number;
  chunk_count: number;
  last_scan_at: string | null;
}

// ---------------------------------------------------------------------------
// Chat history shapes (sidecar SQLite)
// ---------------------------------------------------------------------------

/** A persisted conversation row. The frontend chatStore mirrors this
 *  shape for hydration on app start and for round-tripping renames /
 *  deletes through the local API. `network_mode` is stored as 0/1 in
 *  SQLite; we coerce to bool at the type boundary. */
export interface PersistedConversation {
  id: string;
  title: string;
  project: string | null;
  network_mode: number; // 0/1 from SQLite
  network_model_slug: string | null;
  created_at: string;
  updated_at: string;
  message_count?: number; // present in list endpoint, absent in single-get
}

/** A persisted message row. served_by is null for local-sidecar messages
 *  and "contributor" for network-routed messages (carries the meta from
 *  the SSE stream). */
export interface PersistedMessage {
  id: string;
  conversation_id: string;
  role: 'system' | 'user' | 'assistant';
  content: string;
  status: 'complete' | 'streaming' | 'partial' | 'error';
  served_by: 'contributor' | 'local' | 'household' | null;
  output_tokens: number | null;
  duration_ms: number | null;
  finish_reason: string | null;
  created_at: string;
}

export interface PersistedConversationDetail extends PersistedConversation {
  messages: PersistedMessage[];
}

// ---------------------------------------------------------------------------
// Network (coordinator) shapes
// ---------------------------------------------------------------------------

// ---- Trusted circles -----------------------------------------------------

/** A machine key pasted into a circle by an older app version. Only the owner sees these. */
export interface CircleMember {
  member_pubkey: string;
  added_at: string; // ISO-8601
}

export type CircleRole = 'owner' | 'member';

/** A person with a membership in a circle. `id` is what the owner removes by. */
export interface CirclePerson {
  id: string;
  name: string;
  role: CircleRole;
  is_you: boolean;
  joined_at: string; // ISO-8601
}

/** A trusted circle the calling member owns or has joined. */
export interface Circle {
  id: string;
  name: string;
  created_at: string; // ISO-8601
  /** The caller's own role. */
  role: CircleRole;
  /** Everyone with a membership, owner first. */
  people: CirclePerson[];
  /** Legacy machine keys; the coordinator returns [] to members. */
  members: CircleMember[];
}

/** What a coordinator sends. Older ones omit `role` and `people`; see normalizeCircle. */
export type RawCircle = Omit<Circle, 'role' | 'people' | 'members'> & {
  role?: CircleRole;
  people?: CirclePerson[];
  members?: CircleMember[];
};

/** Response from POST /me/circles/{id}/invites. The code is shown once and never returned again. */
export interface CircleInviteCreated {
  id: string;
  code: string; // e.g. "ABCD-EFGH-JKMN"
  url: string;
  expires_at: string; // ISO-8601
  max_uses: number;
}

/** An invite that can still be accepted, from GET /me/circles/{id}/invites. Never carries the code. */
export interface CircleInvite {
  id: string;
  created_at: string; // ISO-8601
  expires_at: string; // ISO-8601
  max_uses: number;
  uses: number;
}

/** Response from POST /circles/invites/preview: what the invite is for, before anything is joined. */
export interface CircleInvitePreview {
  circle_name: string;
  invited_by: string;
  people: number;
  expires_at: string; // ISO-8601
  already_in: boolean;
}

/** One model a circle can answer with now, and how many of its machines offer it. */
export interface CircleModel {
  slug: string;
  machines: number;
}

/** Response from GET /me/circles/{id}/availability. Counts only; no machine or member is named. */
export interface CircleAvailability {
  circle_id: string;
  models: CircleModel[];
}

/** Response from GET /me/circles, after normalizeCircle. */
export interface ListCirclesResponse {
  circles: Circle[];
}

/** Response from GET /me/identity. */
export interface MyIdentityResponse {
  member_pubkey: string;
}

// ---- Credit ledger -------------------------------------------------------

/**
 * Per-model credit breakdown from GET /me/credit-balance.
 * `earned` and `spent` are Decimal-serialised strings from the server
 * (e.g. "13.50"). Keep them as strings here; format/parse only at the
 * render layer to avoid floating-point precision loss.
 */
export interface CreditBalanceByModel {
  model_slug: string;
  earned: string;
  spent: string;
}

/** Aggregated balance for the calling member. */
export interface CreditBalance {
  member_id: string;
  earned: string;
  spent: string;
  net: string;
  by_model: CreditBalanceByModel[];
}

/**
 * One row from the per-member credit ledger (GET /me/credit-history).
 * `multiplier`, `credits` are Decimal-serialised strings.
 * `prev_row_hash` and `row_hash` are stored but not surfaced in v0 UI.
 */
export interface CreditHistoryEntry {
  id: string;
  direction: 'earned' | 'spent';
  job_id: string;
  model_slug: string;
  output_tokens: number;
  multiplier: string;
  credits: string;
  prev_row_hash: string;
  row_hash: string;
  created_at: string; // ISO-8601 from coordinator
}

export interface CreditHistory {
  member_id: string;
  entries: CreditHistoryEntry[];
  next_cursor: string | null;
}

/** Aggregate fleet summary returned by GET /transparency/fleet. Privacy
 *  invariant: no peer_id / hostname / hardware fields here, by design.
 *  Per-peer identity is the line we don't cross in the requester UI. */
export interface FleetSummary {
  as_of: string;
  online_window_minutes: number;
  peers: {
    active_total: number;
    online_now: number;
    by_platform: Record<string, number>;
    by_version: Record<string, number>;
  };
  models: { slug: string; online_peer_count: number }[];
  activity: {
    jobs_completed_24h: number;
    tokens_served_24h: number;
  };
  releases: {
    trusted_count: number;
    last_signed_at: string | null;
  };
}

/** One of the member's own machines in a contribution receipt. */
export interface ContributionMachine {
  peer_id: string;
  hostname: string | null;
  requests_served: number;
  output_tokens: number;
  /** Decimal-serialised string (e.g. "5.00"); format at the render layer. */
  credits_earned: string;
  /** Canary trust score in [0,1] (neutral 0.5; 0.0 = flagged). null when the
   *  machine has no heartbeat yet. The member's own machine only. */
  reputation: number | null;
}

/** GET /me/contributions - per-machine weekly served + earned, own machines. */
export interface ContributionsResponse {
  member_id: string;
  window_days: number;
  machines: ContributionMachine[];
  total_requests_served: number;
  total_output_tokens: number;
  total_credits_earned: string;
}

export interface MyJobStats {
  jobs_24h: number;
  tokens_received_24h: number;
  avg_duration_ms: number;
}

/** One week bucket in a weekly contribution history. */
export interface ContributionWeek {
  /** ISO date of the Monday that starts this week, e.g. "2026-06-16". */
  week_start: string;
  requests_served: number;
  output_tokens: number;
  /** Decimal-serialised string (e.g. "3.50"); format at the render layer. */
  credits_earned: string;
}

/** GET /me/contributions/weekly - week-by-week history, oldest-first. */
export interface ContributionsWeeklyResponse {
  member_id: string;
  window_weeks: number;
  /** Length == window_weeks; zero-filled weeks are included. */
  weeks: ContributionWeek[];
}

// ---------------------------------------------------------------------------
// Usage dashboard (sidecar SQLite, local-only). Everything here is computed
// and stored on-device; nothing is ever uploaded. Metadata only.
// ---------------------------------------------------------------------------

export type UsageScope = 'local' | 'my_machines' | 'circle' | 'pool';

export interface UsageSummary {
  requests: number;
  input_tokens: number;
  cache_read_tokens: number;
  output_tokens: number;
  total_tokens: number;
  sessions: number;
  active_days: number;
  active_streak: number;
  success_rate: number;
  p95_latency_ms: number;
  avg_ttft_ms: number;
}

export interface UsageActivityDay {
  date: string; // "YYYY-MM-DD"
  tokens: number;
  requests: number;
}

export interface UsageModel {
  model_slug: string;
  tokens: number;
  requests: number;
}

export interface UsagePace {
  last_hour_tokens: number;
  last_24h_tokens: number;
  wow_change_pct: number | null;
  cache_hit_rate: number;
  avg_ttft_ms: number;
}

export interface UsageScopeBucket {
  tokens: number;
  requests: number;
}

export interface UsageSplit {
  window_days: number;
  consumed: Record<UsageScope, UsageScopeBucket>;
  served: Record<UsageScope, UsageScopeBucket>;
  consumed_total: number;
  served_total: number;
}

/** Metadata-only completion record posted for in-app requests the sidecar
 *  could not observe itself (circle / pool / my_machines chat routes renderer
 *  -> coordinator directly). Never carries prompt or response content. */
export interface UsageRecordInput {
  direction?: 'consumed' | 'served';
  scope?: UsageScope;
  model_slug?: string | null;
  input_tokens?: number;
  output_tokens?: number;
  ttft_ms?: number | null;
  duration_ms?: number | null;
  ok?: boolean;
  error_class?: string | null;
}

// ---------------------------------------------------------------------------
// API surface
// ---------------------------------------------------------------------------

export const localApi = {
  modelAvailability(signal?: AbortSignal): Promise<ModelAvailability> {
    return timedFetch(`${LOCAL_BASE}/models/available`, signal ? { signal } : {}).then(handleResponse<ModelAvailability>);
  },
  splitStatus(signal?: AbortSignal): Promise<SplitStatus> {
    return timedFetch(`${LOCAL_BASE}/split/status`, signal ? { signal } : {}).then(handleResponse<SplitStatus>);
  },
  modelIdentity(slug: string): Promise<SplitModelPin> {
    return timedFetch(`${LOCAL_BASE}/models/${encodeURIComponent(slug)}/identity`, {}, LOAD_TIMEOUT_MS)
      .then(handleResponse<{ model: SplitModelPin }>).then(result => result.model);
  },
  splitPlan(selection: SplitSelection): Promise<unknown> {
    return timedFetch(`${LOCAL_BASE}/split/plan`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(selection) }).then(handleResponse);
  },
  splitStart(selection: SplitSelection): Promise<SplitStatus> {
    return timedFetch(`${LOCAL_BASE}/split/start`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(selection) }).then(handleResponse<SplitStatus>);
  },
  splitStop(): Promise<SplitStatus> {
    return timedFetch(`${LOCAL_BASE}/split/stop`, { method: 'POST' }).then(handleResponse<SplitStatus>);
  },
  /** GET /health - liveness check; also tells us if a model is loaded */
  health(): Promise<HealthResponse> {
    return timedFetch(`${LOCAL_BASE}/health`).then((r) => handleResponse<HealthResponse>(r));
  },
  /**
   * GET /health/inference: backend, devices and, from engines that check the
   * model's answers, the `acceptance` result. Unlike /health this route needs
   * the engine token; Electron main attaches it to every renderer request to
   * the engine, as for the other routes. Returns the raw body: lib/acceptance.ts
   * reads only the part the app shows.
   */
  inferenceHealth(): Promise<Record<string, unknown>> {
    return timedFetch(`${LOCAL_BASE}/health/inference`).then((r) =>
      handleResponse<Record<string, unknown>>(r),
    );
  },

  // ---- Chat ----------------------------------------------------------------

  /**
   * POST /chat/completions with stream: true.
   * Returns the raw Response so the caller can pipe the body through
   * lib/streaming.ts to yield tokens as an async generator.
   */
  streamChat(messages: ChatMessage[], options: ChatOptions): Promise<Response> {
    const init: RequestInit = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: messages.map(({ role, content }) => ({ role, content })),
        stream: true,
        temperature: options.temperature ?? 0.7,
        max_tokens: options.maxTokens ?? 2048,
      }),
    };
    if (options.abortSignal) init.signal = options.abortSignal;
    // Streaming SSE - raw fetch, no timeout (long-lived); caller owns the abort.
    return nativeFetch(`${LOCAL_BASE}/chat/completions`, init);
  },

  /**
   * POST /jobs/stream against the coordinator (not the local sidecar), or for
   * a circle chat POST /chat/circle on the local engine, which answers with
   * the coordinator's own shapes and falls back to the coordinator when the
   * engine cannot take it (see streamCircleJob).
   * Returns a Response whose body is an OpenAI-compatible SSE stream
   * - same shape /chat/completions emits, so streamTokens() consumes
   *   it without any modification.
   *
   * Routing scope is always explicit: a selected circle must never inherit
   * the coordinator's pool default. The open pool gets only the latest
   * prompt. A trusted circle also gets the conversation so far, so a
   * follow-up makes sense to the machine that answers; history never rides
   * on a request without a circle.
   */
  streamNetworkJob(
    prompt: string,
    options: {
      modelSlug: string;
      temperature?: number;
      maxTokens?: number;
      abortSignal?: AbortSignal;
      /** When set, the coordinator only routes to members of this circle. */
      circleId?: string;
      /** Conversation so far (role + content only). Sent to a circle only. */
      messages?: { role: string; content: string }[];
    },
  ): Promise<Response> {
    const body: Record<string, unknown> = {
      prompt,
      model_slug: options.modelSlug,
      scope: options.circleId !== undefined ? 'circle' : 'pool',
      sampling: {
        temperature: options.temperature ?? 0.7,
        max_tokens: options.maxTokens ?? 2048,
      },
    };
    if (options.circleId !== undefined) {
      body['circle_id'] = options.circleId;
      if (options.messages !== undefined && options.messages.length > 0) {
        body['messages'] = options.messages.map((m) => ({ role: m.role, content: m.content }));
      }
    }
    const init: RequestInit = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    };
    if (options.abortSignal) init.signal = options.abortSignal;
    return options.circleId !== undefined ? streamCircleJob(init) : streamViaCoordinator(init);
  },

  // ---- Models --------------------------------------------------------------

  listModels(): Promise<Model[]> {
    return timedFetch(`${LOCAL_BASE}/models`).then((r) => handleResponse<Model[]>(r));
  },

  installModel(id: string): Promise<{ status: string }> {
    return timedFetch(`${LOCAL_BASE}/models/${encodeURIComponent(id)}/install`, {
      method: 'POST',
    }).then((r) => handleResponse<{ status: string }>(r));
  },

  deleteModel(id: string): Promise<{ status: string }> {
    return timedFetch(`${LOCAL_BASE}/models/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }).then((r) => handleResponse<{ status: string }>(r));
  },

  loadModel(
    id: string,
    options: {
      rpc_servers?: string[];
      /** When set, loads with the pipeline-parallel strategy: the sidecar
       *  auto-orchestrates the household (picks pipeline-eligible peers,
       *  starts their stages) and runs the model split across them. `true`
       *  uses defaults; pass an object to pin device/dtype/max nodes. */
      pipeline?: boolean | { device?: string; dtype?: string; max_nodes?: number };
      /** Opt-in shared (continuous) batching for the transformers paths
       *  (whole-model / pipeline): concurrent requests share batched forward
       *  passes instead of serialising. Default off -> single-stream path is
       *  byte-for-byte unchanged. Ignored on the llama.cpp/GGUF path. */
      continuousBatch?: boolean;
    } = {},
  ): Promise<{
    model_path: string;
    load_time_seconds: number;
    rpc_servers?: string[] | null;
    strategy?: string;
    chain?: unknown;
    continuous_batch?: boolean;
  }> {
    // Two distributed strategies share this endpoint; an empty body loads locally.
    const body: Record<string, unknown> = {};
    if (options.pipeline) {
      // Pipeline-parallel - the sidecar orchestrates the household chain itself.
      body['pipeline_auto'] = options.pipeline === true ? {} : options.pipeline;
    } else if (options.rpc_servers && options.rpc_servers.length > 0) {
      // Big Model Mode - remote llama.cpp RPC backends as ggml devices. Requires
      // the Potluck-patched llama-cpp-python wheel; the sidecar surfaces a 501 if not.
      body['rpc_servers'] = options.rpc_servers;
    }
    // Opt-in only - when off, omit it so a plain local load still sends no body.
    // The sidecar ignores it on the GGUF path.
    if (options.continuousBatch) body['continuous_batch'] = true;
    const hasBody = Object.keys(body).length > 0;
    return timedFetch(`${LOCAL_BASE}/models/${encodeURIComponent(id)}/load`, {
      method: 'POST',
      ...(hasBody
        ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
        : {}),
    }, LOAD_TIMEOUT_MS).then((r) =>
      handleResponse<{
        model_path: string;
        load_time_seconds: number;
        rpc_servers?: string[] | null;
        strategy?: string;
        chain?: unknown;
        continuous_batch?: boolean;
      }>(r),
    );
  },

  /** Benchmark the currently loaded model: real tokens/sec + time-to-first-token. */
  benchmarkModel(): Promise<BenchmarkResult> {
    return timedFetch(`${LOCAL_BASE}/models/benchmark`, { method: 'POST' }, LOAD_TIMEOUT_MS).then(
      (r) => handleResponse<BenchmarkResult>(r),
    );
  },

  downloadProgress(id: string): Promise<DownloadProgress> {
    return timedFetch(`${LOCAL_BASE}/models/${encodeURIComponent(id)}/download-progress`).then((r) =>
      handleResponse<DownloadProgress>(r),
    );
  },

  // ---- Hardware ------------------------------------------------------------

  detectHardware(): Promise<Record<string, unknown>> {
    return timedFetch(`${LOCAL_BASE}/hardware`).then((r) =>
      handleResponse<Record<string, unknown>>(r),
    );
  },

  /**
   * GET /readiness/gpu-test - run a real device op and return whether the
   * GPU/runtime actually computed. Uses LOAD_TIMEOUT_MS because an inference
   * test can take several seconds on a cold runtime.
   *
   * This is an action: call only on explicit user gesture, never on a poll.
   */
  gpuTest(): Promise<GpuTestResult> {
    return timedFetch(`${LOCAL_BASE}/readiness/gpu-test`, {}, LOAD_TIMEOUT_MS).then((r) =>
      handleResponse<GpuTestResult>(r),
    );
  },

  // ---- Settings ------------------------------------------------------------

  getSettings(): Promise<PotluckSettings> {
    return timedFetch(`${LOCAL_BASE}/settings`).then((r) => handleResponse<PotluckSettings>(r));
  },

  /** GET /contributor/status - what the Contribute compute toggle is doing
   *  (state, this machine's own peer id, counters). Never lists other peers. */
  getContributorStatus(): Promise<ContributorStatus> {
    return timedFetch(`${LOCAL_BASE}/contributor/status`).then((r) =>
      handleResponse<ContributorStatus>(r),
    );
  },

  updateSettings(patch: PotluckSettingsPatch): Promise<PotluckSettings> {
    return timedFetch(`${LOCAL_BASE}/settings`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).then((r) => handleResponse<PotluckSettings>(r));
  },

  /** Generate + persist a fresh gateway API key (enables the gateway). Returns
   *  the new key for one-time display. Invalidates the previous key. */
  rotateGatewayKey(): Promise<{ enabled: boolean; api_key: string }> {
    return timedFetch(`${LOCAL_BASE}/settings/gateway/rotate`, {
      method: 'POST',
    }).then((r) => handleResponse<{ enabled: boolean; api_key: string }>(r));
  },

  // ---- Memory --------------------------------------------------------------

  memory: {
    list(opts: {
      category?: MemoryCategory;
      limit?: number;
      offset?: number;
      project?: string | null;
      include_all?: boolean;
    } = {}): Promise<Memory[]> {
      const params = new URLSearchParams();
      if (opts.category !== undefined) params.set('category', opts.category);
      if (opts.limit !== undefined) params.set('limit', String(opts.limit));
      if (opts.offset !== undefined) params.set('offset', String(opts.offset));
      if (opts.project !== undefined && opts.project !== null) params.set('project', opts.project);
      if (opts.include_all !== undefined) params.set('include_all', opts.include_all ? 'true' : 'false');
      const qs = params.toString();
      return timedFetch(`${LOCAL_BASE}/memory${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<Memory[]>(r),
      );
    },

    projects(): Promise<ProjectSummary[]> {
      return timedFetch(`${LOCAL_BASE}/memory/projects`).then((r) =>
        handleResponse<ProjectSummary[]>(r),
      );
    },

    get(id: string): Promise<Memory> {
      return timedFetch(`${LOCAL_BASE}/memory/${encodeURIComponent(id)}`).then((r) =>
        handleResponse<Memory>(r),
      );
    },

    create(body: {
      content: string;
      category: MemoryCategory;
      conversation_id?: string;
      confidence?: number;
      project?: string | null;
    }): Promise<Memory> {
      return timedFetch(`${LOCAL_BASE}/memory`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<Memory>(r));
    },

    update(
      id: string,
      patch: { content?: string; category?: MemoryCategory; confidence?: number; project?: string | null },
    ): Promise<Memory> {
      return timedFetch(`${LOCAL_BASE}/memory/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }).then((r) => handleResponse<Memory>(r));
    },

    delete(id: string): Promise<void> {
      return timedFetch(`${LOCAL_BASE}/memory/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      }).then((r) => handle204(r));
    },

    pin(id: string, pinned: boolean): Promise<{ id: string; pinned: boolean }> {
      return timedFetch(`${LOCAL_BASE}/memory/${encodeURIComponent(id)}/pin`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pinned }),
      }).then((r) => handleResponse<{ id: string; pinned: boolean }>(r));
    },

    search(body: {
      query?: string;
      current_messages?: ChatMessage[];
      max_memories?: number;
      min_confidence?: number;
      project?: string | null;
      semantic?: boolean;
    }): Promise<Memory[]> {
      return timedFetch(`${LOCAL_BASE}/memory/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<Memory[]>(r));
    },

    related(id: string, limit = 8): Promise<RelatedMemory[]> {
      const params = new URLSearchParams({ limit: String(limit) });
      return timedFetch(
        `${LOCAL_BASE}/memory/${encodeURIComponent(id)}/related?${params.toString()}`,
      ).then((r) => handleResponse<RelatedMemory[]>(r));
    },

    keywords(opts: {
      limit?: number;
      project?: string;
      min_count?: number;
    } = {}): Promise<KeywordFrequency[]> {
      const params = new URLSearchParams();
      if (opts.limit !== undefined) params.set('limit', String(opts.limit));
      if (opts.project !== undefined && opts.project !== '') params.set('project', opts.project);
      if (opts.min_count !== undefined) params.set('min_count', String(opts.min_count));
      const qs = params.toString();
      return timedFetch(`${LOCAL_BASE}/memory/keywords${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<KeywordFrequency[]>(r),
      );
    },

    extract(body: { messages: ChatMessage[]; conversation_id: string }): Promise<Memory[]> {
      return timedFetch(`${LOCAL_BASE}/memory/extract`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }, EXTRACT_TIMEOUT_MS).then((r) => handleResponse<Memory[]>(r));
    },

    wipe(): Promise<MemoryWipeResult> {
      return timedFetch(`${LOCAL_BASE}/memory/wipe`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true }),
      }).then((r) => handleResponse<MemoryWipeResult>(r));
    },

    export(): Promise<MemoryExportResult> {
      return timedFetch(`${LOCAL_BASE}/memory/export`).then((r) =>
        handleResponse<MemoryExportResult>(r),
      );
    },

    import(body: { memories: Memory[] }): Promise<MemoryImportResult> {
      return timedFetch(`${LOCAL_BASE}/memory/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<MemoryImportResult>(r));
    },

    clusters(opts: { project?: string } = {}): Promise<ClusterResponse> {
      const params = new URLSearchParams();
      if (opts.project !== undefined && opts.project !== '') params.set('project', opts.project);
      const qs = params.toString();
      return timedFetch(`${LOCAL_BASE}/memory/clusters${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<ClusterResponse>(r),
      );
    },
  },

  // ---- Tasks ---------------------------------------------------------------

  tasks: {
    list(opts: { status?: TaskStatus; project?: string | null; include_all?: boolean } = {}): Promise<TaskState[]> {
      const params = new URLSearchParams();
      if (opts.status !== undefined) params.set('status', opts.status);
      if (opts.project !== undefined && opts.project !== null) params.set('project', opts.project);
      if (opts.include_all !== undefined) params.set('include_all', opts.include_all ? 'true' : 'false');
      const qs = params.toString();
      return timedFetch(`${LOCAL_BASE}/tasks${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<TaskState[]>(r),
      );
    },

    get(id: string): Promise<TaskState> {
      return timedFetch(`${LOCAL_BASE}/tasks/${encodeURIComponent(id)}`).then((r) =>
        handleResponse<TaskState>(r),
      );
    },

    create(body: TaskCreateInput): Promise<TaskState> {
      return timedFetch(`${LOCAL_BASE}/tasks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<TaskState>(r));
    },

    update(id: string, patch: TaskUpdateInput): Promise<TaskState> {
      return timedFetch(`${LOCAL_BASE}/tasks/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }).then((r) => handleResponse<TaskState>(r));
    },

    completeStep(id: string, step: string): Promise<TaskState> {
      return timedFetch(`${LOCAL_BASE}/tasks/${encodeURIComponent(id)}/complete-step`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ step }),
      }).then((r) => handleResponse<TaskState>(r));
    },

    addError(id: string, body: { error: string; resolution?: string }): Promise<TaskState> {
      return timedFetch(`${LOCAL_BASE}/tasks/${encodeURIComponent(id)}/add-error`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<TaskState>(r));
    },

    delete(id: string): Promise<void> {
      return timedFetch(`${LOCAL_BASE}/tasks/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      }).then((r) => handle204(r));
    },
  },

  // ---- Agents --------------------------------------------------------------

  agents: {
    managedCapabilities(): Promise<ManagedAgentCapabilities> {
      return timedFetch(`${LOCAL_BASE}/agents/managed/capabilities`).then((r) =>
        handleResponse<ManagedAgentCapabilities>(r),
      );
    },

    listManagedSessions(taskId?: string): Promise<ManagedAgentSession[]> {
      const params = new URLSearchParams();
      if (taskId) params.set('task_id', taskId);
      const query = params.toString();
      return timedFetch(`${LOCAL_BASE}/agents/managed/sessions${query ? `?${query}` : ''}`).then(
        (r) => handleResponse<ManagedAgentSession[]>(r),
      );
    },

    startManagedSession(input: {
      task_id: string;
      cwd: string;
      model?: string;
      sandbox: ManagedSandboxMode;
      direction?: string;
    }): Promise<ManagedAgentSession> {
      return timedFetch(`${LOCAL_BASE}/agents/managed/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(input),
      }, 60_000).then((r) => handleResponse<ManagedAgentSession>(r));
    },

    getManagedSession(id: string): Promise<ManagedAgentSession> {
      return timedFetch(`${LOCAL_BASE}/agents/managed/sessions/${encodeURIComponent(id)}`).then(
        (r) => handleResponse<ManagedAgentSession>(r),
      );
    },

    sendManagedMessage(id: string, text: string): Promise<ManagedAgentSession> {
      return timedFetch(`${LOCAL_BASE}/agents/managed/sessions/${encodeURIComponent(id)}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      }, 60_000).then((r) => handleResponse<ManagedAgentSession>(r));
    },

    steerManagedSession(id: string, text: string): Promise<ManagedAgentSession> {
      return timedFetch(`${LOCAL_BASE}/agents/managed/sessions/${encodeURIComponent(id)}/steer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      }).then((r) => handleResponse<ManagedAgentSession>(r));
    },

    interruptManagedSession(id: string): Promise<ManagedAgentSession> {
      return timedFetch(`${LOCAL_BASE}/agents/managed/sessions/${encodeURIComponent(id)}/interrupt`, {
        method: 'POST',
      }).then((r) => handleResponse<ManagedAgentSession>(r));
    },

    resolveManagedApproval(
      id: string,
      approvalId: string,
      decision: ManagedApprovalDecision,
    ): Promise<ManagedAgentSession> {
      return timedFetch(
        `${LOCAL_BASE}/agents/managed/sessions/${encodeURIComponent(id)}/approvals/${encodeURIComponent(approvalId)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ decision }),
        },
      ).then((r) => handleResponse<ManagedAgentSession>(r));
    },

    connectorStatus(provider: AgentProvider, projectPath: string): Promise<AgentConnectorStatus> {
      const params = new URLSearchParams({ project_path: projectPath });
      return timedFetch(
        `${LOCAL_BASE}/agents/connectors/${provider}?${params.toString()}`,
      ).then((r) => handleResponse<AgentConnectorStatus>(r));
    },

    connectProvider(provider: AgentProvider, projectPath: string): Promise<AgentConnectorStatus> {
      return timedFetch(`${LOCAL_BASE}/agents/connectors/${provider}/connect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_path: projectPath }),
      }).then((r) => handleResponse<AgentConnectorStatus>(r));
    },

    disconnectProvider(provider: AgentProvider, projectPath: string): Promise<AgentConnectorStatus> {
      return timedFetch(`${LOCAL_BASE}/agents/connectors/${provider}/disconnect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_path: projectPath }),
      }).then((r) => handleResponse<AgentConnectorStatus>(r));
    },

    listRuns(opts: { status?: string; provider?: string; project?: string; limit?: number } = {}): Promise<AgentRun[]> {
      const params = new URLSearchParams();
      if (opts.status) params.set('status', opts.status);
      if (opts.provider) params.set('provider', opts.provider);
      if (opts.project) params.set('project', opts.project);
      if (opts.limit !== undefined) params.set('limit', String(opts.limit));
      const qs = params.toString();
      return timedFetch(`${LOCAL_BASE}/agents/runs${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<AgentRun[]>(r),
      );
    },

    getRun(id: string): Promise<AgentRunDetail> {
      return timedFetch(`${LOCAL_BASE}/agents/runs/${encodeURIComponent(id)}`).then((r) =>
        handleResponse<AgentRunDetail>(r),
      );
    },

    getRunEvents(id: string, afterSeq: number): Promise<AgentRunEventDelta> {
      const params = new URLSearchParams({ after_seq: String(afterSeq) });
      return timedFetch(
        `${LOCAL_BASE}/agents/runs/${encodeURIComponent(id)}/events?${params.toString()}`,
      ).then((r) => handleResponse<AgentRunEventDelta>(r));
    },

    listSessions(opts: { include_disconnected?: boolean; limit?: number } = {}): Promise<AgentSession[]> {
      const params = new URLSearchParams();
      if (opts.include_disconnected !== undefined) {
        params.set('include_disconnected', opts.include_disconnected ? 'true' : 'false');
      }
      if (opts.limit !== undefined) params.set('limit', String(opts.limit));
      const qs = params.toString();
      return timedFetch(`${LOCAL_BASE}/agents/sessions${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<AgentSession[]>(r),
      );
    },

    getSession(id: string): Promise<SessionDetail> {
      return timedFetch(`${LOCAL_BASE}/agents/sessions/${encodeURIComponent(id)}`).then((r) =>
        handleResponse<SessionDetail>(r),
      );
    },

    activity(opts: { limit?: number; client?: string; project?: string; tool?: string } = {}): Promise<ToolCall[]> {
      const params = new URLSearchParams();
      if (opts.limit !== undefined) params.set('limit', String(opts.limit));
      if (opts.client) params.set('client', opts.client);
      if (opts.project) params.set('project', opts.project);
      if (opts.tool) params.set('tool', opts.tool);
      const qs = params.toString();
      return timedFetch(`${LOCAL_BASE}/agents/activity${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<ToolCall[]>(r),
      );
    },

    deleteSession(id: string): Promise<void> {
      return timedFetch(`${LOCAL_BASE}/agents/sessions/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      }).then((r) => handle204(r));
    },

    clearDisconnected(): Promise<{ removed: number }> {
      return timedFetch(`${LOCAL_BASE}/agents/sessions`, { method: 'DELETE' }).then((r) =>
        handleResponse<{ removed: number }>(r),
      );
    },

    /** Trigger a one-shot stale-session sweep. Marks any active session
     *  whose last_seen_at is > 5 min old as disconnected, and DELETES rows
     *  that never identified themselves and have zero tool calls older
     *  than 30 min. Returns counts. Same sweep runs every 60s automatically;
     *  this is the immediate-feedback path from the AgentsView clean-up
     *  button. */
    sweep(): Promise<{ marked_disconnected: number; deleted_worthless: number }> {
      return timedFetch(`${LOCAL_BASE}/agents/sessions/sweep`, { method: 'POST' }).then((r) =>
        handleResponse<{ marked_disconnected: number; deleted_worthless: number }>(r),
      );
    },

    /** Resolve a batch of memory ids to lightweight previews for the
     *  debugger's memory-overlay panel. Capped server-side at 100 ids per
     *  call. Missing ids are silently dropped - the panel renders that as
     *  "memory deleted since this call". */
    memoryPreviews(ids: string[]): Promise<MemoryPreview[]> {
      if (ids.length === 0) return Promise.resolve([]);
      return timedFetch(`${LOCAL_BASE}/agents/memory-previews`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      }).then((r) => handleResponse<MemoryPreview[]>(r));
    },

    /** Download a sanitized portable JSON trace of one session. The default
     *  omits tool argument summaries and memory previews.
     *  Returns the response as a Blob (not decoded JSON) so the caller can
     *  trigger a file save while preserving the server's filename hint.
     *  See the /trace endpoint for the privacy guarantees. */
    async exportSessionTrace(
      sessionId: string,
    ): Promise<{ blob: Blob; filename: string }> {
      const res = await timedFetch(
        `${LOCAL_BASE}/agents/sessions/${encodeURIComponent(sessionId)}/trace`,
      );
      if (!res.ok) throw new Error(`Trace export failed: HTTP ${res.status}`);
      const blob = await res.blob();
      const cd = res.headers.get('Content-Disposition') ?? '';
      const match = /filename="([^"]+)"/.exec(cd);
      const filename = match?.[1] ?? `potluck-trace-${sessionId}.json`;
      return { blob, filename };
    },
  },

  // ---- Usage dashboard (sidecar SQLite, local-only) ------------------------

  usage: {
    summary(windowDays = 30): Promise<UsageSummary> {
      return timedFetch(`${LOCAL_BASE}/usage/summary?window=${windowDays}`).then((r) =>
        handleResponse<UsageSummary>(r),
      );
    },

    activity(rangeDays = 365): Promise<UsageActivityDay[]> {
      return timedFetch(`${LOCAL_BASE}/usage/activity?range=${rangeDays}`).then((r) =>
        handleResponse<UsageActivityDay[]>(r),
      );
    },

    models(limit = 8): Promise<UsageModel[]> {
      return timedFetch(`${LOCAL_BASE}/usage/models?limit=${limit}`).then((r) =>
        handleResponse<UsageModel[]>(r),
      );
    },

    pace(): Promise<UsagePace> {
      return timedFetch(`${LOCAL_BASE}/usage/pace`).then((r) => handleResponse<UsagePace>(r));
    },

    split(windowDays = 30): Promise<UsageSplit> {
      return timedFetch(`${LOCAL_BASE}/usage/split?window=${windowDays}`).then((r) =>
        handleResponse<UsageSplit>(r),
      );
    },

    /** Persist a completed in-app request the sidecar could not observe (see
     *  UsageRecordInput). Fire-and-forget: telemetry must never break a chat,
     *  so callers ignore the result and swallow errors. */
    record(input: UsageRecordInput): Promise<{ ok: boolean }> {
      return timedFetch(`${LOCAL_BASE}/usage/record`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(input),
      }).then((r) => handleResponse<{ ok: boolean }>(r));
    },
  },

  // ---- Chat history (sidecar SQLite, local-only) ---------------------------

  history: {
    list(opts: { project?: string | null; include_all?: boolean; limit?: number } = {}): Promise<PersistedConversation[]> {
      const params = new URLSearchParams();
      if (opts.include_all !== undefined) params.set('include_all', opts.include_all ? 'true' : 'false');
      if (opts.project !== undefined && opts.project !== null) params.set('project', opts.project);
      if (opts.limit !== undefined) params.set('limit', String(opts.limit));
      const qs = params.toString();
      return timedFetch(`${LOCAL_BASE}/chat/conversations${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<PersistedConversation[]>(r),
      );
    },

    get(id: string): Promise<PersistedConversationDetail> {
      return timedFetch(`${LOCAL_BASE}/chat/conversations/${encodeURIComponent(id)}`).then((r) =>
        handleResponse<PersistedConversationDetail>(r),
      );
    },

    create(body: {
      id?: string;
      title?: string;
      project?: string | null;
      network_mode?: boolean;
      network_model_slug?: string | null;
    }): Promise<PersistedConversation> {
      return timedFetch(`${LOCAL_BASE}/chat/conversations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<PersistedConversation>(r));
    },

    update(id: string, patch: {
      title?: string;
      network_mode?: boolean;
      network_model_slug?: string | null;
      project?: string | null;
      project_set?: boolean;
    }): Promise<PersistedConversation> {
      return timedFetch(`${LOCAL_BASE}/chat/conversations/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }).then((r) => handleResponse<PersistedConversation>(r));
    },

    delete(id: string): Promise<void> {
      return timedFetch(`${LOCAL_BASE}/chat/conversations/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      }).then((r) => handle204(r));
    },

    appendMessage(conversationId: string, body: {
      id?: string;
      role: 'user' | 'assistant' | 'system';
      content: string;
      status?: 'complete' | 'streaming' | 'partial' | 'error';
      served_by?: 'contributor' | 'local' | 'household' | null;
      output_tokens?: number | null;
      duration_ms?: number | null;
      finish_reason?: string | null;
    }): Promise<PersistedMessage> {
      return timedFetch(`${LOCAL_BASE}/chat/conversations/${encodeURIComponent(conversationId)}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<PersistedMessage>(r));
    },

    updateMessage(messageId: string, patch: {
      content?: string;
      status?: 'complete' | 'streaming' | 'partial' | 'error';
      served_by?: 'contributor' | 'local' | 'household' | null;
      output_tokens?: number | null;
      duration_ms?: number | null;
      finish_reason?: string | null;
    }): Promise<{ id: string; updated: boolean }> {
      return timedFetch(`${LOCAL_BASE}/chat/messages/${encodeURIComponent(messageId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }).then((r) => handleResponse<{ id: string; updated: boolean }>(r));
    },

    clearMessages(conversationId: string): Promise<{ removed: number }> {
      return timedFetch(`${LOCAL_BASE}/chat/conversations/${encodeURIComponent(conversationId)}/messages`, {
        method: 'DELETE',
      }).then((r) => handleResponse<{ removed: number }>(r));
    },
  },

  // ---- Network (coordinator-side fleet + per-member stats) ---------------

  network: {
    /** GET /transparency/fleet on the coordinator. Aggregate-only - no
     *  peer_id / hostname / individual peer fields, by design.
     *  coordinator-bare-fetch-ok: no auth dependency server-side (see
     *  coordinator/routers/transparency.py) - a public transparency route,
     *  aggregate fleet stats only, no per-member data. */
    fleet(): Promise<FleetSummary> {
      return timedFetch(`${coordinatorBase()}/transparency/fleet`).then((r) =>
        handleResponse<FleetSummary>(r),
      );
    },

    /** GET /health on the coordinator. Used as a cheap reachability probe
     *  to gate UI affordances that depend on the coordinator (e.g. the
     *  chat composer's Network-mode toggle). Resolves to `true` only when
     *  the response is a 2xx; any other outcome (5xx, network error,
     *  timeout) resolves to `false` rather than throwing - callers should
     *  not need a try/catch for this.
     *  coordinator-bare-fetch-ok: /health has no auth dependency server-side
     *  (see coordinator/routers/health.py) - a public liveness probe, and
     *  the only way to gate the toggle before a session may even exist. */
    health(timeoutMs = 2000): Promise<boolean> {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      return nativeFetch(`${coordinatorBase()}/health`, { signal: controller.signal })
        .then((r) => r.ok)
        .catch(() => false)
        .finally(() => clearTimeout(timer));
    },

    /** GET /jobs/stats on the coordinator. Returns this member's
     *  requester-side counts + average latency for the last 24h.
     *  Session-authenticated - see coordinatorAuth.ts. The server does not
     *  also gate this route on the coordinator token, since it is called
     *  directly by the frontend, which can never hold the coordinator's
     *  shared secret - see coordinator/routers/jobs.py. */
    myStats(): Promise<MyJobStats> {
      return coordinatorSessionRequest('GET', '/jobs/stats').then((r) =>
        fromSessionResult<MyJobStats>(r),
      );
    },

    /** GET /me/contributions - per-machine served + earned for the member's
     *  own machines over the last `days` (default 7). Own-machine data only.
     *  Session-authenticated - see coordinatorAuth.ts. */
    myContributions(days = 7): Promise<ContributionsResponse> {
      return coordinatorSessionRequest('GET', `/me/contributions?days=${days}`).then((r) =>
        fromSessionResult<ContributionsResponse>(r),
      );
    },

    /** GET /me/contributions/weekly - week-by-week history, oldest-first.
     *  Returns `weeks` weeks of data (zero-filled); default 8.
     *  Session-authenticated - see coordinatorAuth.ts. */
    myContributionsWeekly(weeks = 8): Promise<ContributionsWeeklyResponse> {
      return coordinatorSessionRequest('GET', `/me/contributions/weekly?weeks=${weeks}`).then((r) =>
        fromSessionResult<ContributionsWeeklyResponse>(r),
      );
    },
  },

  // ---- Credits (coordinator ledger) --------------------------------------
  // Session-authenticated (not the optional coordinator token) - see
  // coordinatorAuth.ts. Both routes are under /me/*, which the prod
  // coordinator requires a member session for.

  credits: {
    /** GET /me/credit-balance - aggregated earned / spent / net plus
     *  per-model breakdown. Decimal fields arrive as strings; keep them
     *  that way - parse/format only at the render layer. */
    balance(): Promise<CreditBalance> {
      return coordinatorSessionRequest('GET', '/me/credit-balance').then((r) =>
        fromSessionResult<CreditBalance>(r),
      );
    },

    /** GET /me/credit-history - newest-first ledger entries.
     *  `direction` filters to 'earned' or 'spent'; omit for all.
     *  `limit` defaults to 50 server-side; pass 10 for the glance widget. */
    history(opts: { direction?: 'earned' | 'spent'; limit?: number } = {}): Promise<CreditHistory> {
      const params = new URLSearchParams();
      if (opts.direction !== undefined) params.set('direction', opts.direction);
      if (opts.limit !== undefined) params.set('limit', String(opts.limit));
      const qs = params.toString();
      return coordinatorSessionRequest('GET', `/me/credit-history${qs ? `?${qs}` : ''}`).then((r) =>
        fromSessionResult<CreditHistory>(r),
      );
    },
  },

  // ---- Trusted circles (coordinator) ---------------------------------------

  // Session-authenticated (not the optional coordinator token) - see
  // coordinatorAuth.ts. The prod coordinator requires a member session for
  // every /me/* route, so these go through the bearer-attaching bridge
  // rather than a bare fetch.
  circles: {
    /** GET /me/circles: the circles the calling member owns or has joined. */
    list(): Promise<ListCirclesResponse> {
      return coordinatorSessionRequest('GET', '/me/circles').then((r) => {
        const body = fromSessionResult<{ circles: RawCircle[] }>(r);
        return { circles: body.circles.map(normalizeCircle) };
      });
    },

    /** POST /me/circles { name } - create a new trusted circle. */
    create(name: string): Promise<Circle> {
      return coordinatorSessionRequest('POST', '/me/circles', { name }).then((r) =>
        normalizeCircle(fromSessionResult<RawCircle>(r)),
      );
    },

    /** DELETE /me/circles/{circle_id}: delete a circle and all its members. Owner only. */
    delete(circleId: string): Promise<void> {
      return coordinatorSessionRequest(
        'DELETE',
        `/me/circles/${encodeURIComponent(circleId)}`,
      ).then((r) => fromSessionResult<void>(r));
    },

    /** POST /me/circles/{circle_id}/members { member_pubkey }: add a machine key.
     *  Kept for older circles; the Circles view no longer offers it. */
    addMember(circleId: string, memberPubkey: string): Promise<CircleMember> {
      return coordinatorSessionRequest(
        'POST',
        `/me/circles/${encodeURIComponent(circleId)}/members`,
        { member_pubkey: memberPubkey },
      ).then((r) => fromSessionResult<CircleMember>(r));
    },

    /** GET /me/circles/{circle_id}/availability - models the circle can run right now. */
    availability(circleId: string): Promise<CircleAvailability> {
      return coordinatorSessionRequest(
        'GET',
        `/me/circles/${encodeURIComponent(circleId)}/availability`,
      ).then((r) => fromSessionResult<CircleAvailability>(r));
    },

    /** DELETE /me/circles/{circle_id}/members/{member_pubkey}: remove a legacy machine key. */
    removeMember(circleId: string, memberPubkey: string): Promise<void> {
      return coordinatorSessionRequest(
        'DELETE',
        `/me/circles/${encodeURIComponent(circleId)}/members/${encodeURIComponent(memberPubkey)}`,
      ).then((r) => fromSessionResult<void>(r));
    },

    /** POST /me/circles/{circle_id}/invites: owner makes an invite. Defaults are 7 days
     *  and one use. The code comes back once; the coordinator never returns it again. */
    createInvite(
      circleId: string,
      opts: { expires_in_days?: number; max_uses?: number } = {},
    ): Promise<CircleInviteCreated> {
      return coordinatorSessionRequest(
        'POST',
        `/me/circles/${encodeURIComponent(circleId)}/invites`,
        opts,
      ).then((r) => fromSessionResult<CircleInviteCreated>(r));
    },

    /** GET /me/circles/{circle_id}/invites: invites that can still be accepted, without codes. */
    listInvites(circleId: string): Promise<CircleInvite[]> {
      return coordinatorSessionRequest(
        'GET',
        `/me/circles/${encodeURIComponent(circleId)}/invites`,
      ).then((r) => fromSessionResult<CircleInvite[]>(r));
    },

    /** DELETE /me/circles/{circle_id}/invites/{invite_id}: withdraw an invite. */
    revokeInvite(circleId: string, inviteId: string): Promise<void> {
      return coordinatorSessionRequest(
        'DELETE',
        `/me/circles/${encodeURIComponent(circleId)}/invites/${encodeURIComponent(inviteId)}`,
      ).then((r) => fromSessionResult<void>(r));
    },

    /** POST /circles/invites/preview { code }: what an invite is for, without joining.
     *  `code` may be the code in any case, with or without dashes, or the whole link. */
    previewInvite(code: string): Promise<CircleInvitePreview> {
      return coordinatorSessionRequest('POST', '/circles/invites/preview', { code }).then((r) =>
        fromSessionResult<CircleInvitePreview>(r),
      );
    },

    /** POST /circles/invites/accept { code }: join the circle the invite is for.
     *  Throws [localApi] 404 for a bad or used code and 409 when the circle is full. */
    acceptInvite(code: string): Promise<Circle> {
      return coordinatorSessionRequest('POST', '/circles/invites/accept', { code }).then((r) =>
        normalizeCircle(fromSessionResult<RawCircle>(r)),
      );
    },

    /** DELETE /me/circles/{circle_id}/membership: a member leaves. The owner gets a 409. */
    leave(circleId: string): Promise<void> {
      return coordinatorSessionRequest(
        'DELETE',
        `/me/circles/${encodeURIComponent(circleId)}/membership`,
      ).then((r) => fromSessionResult<void>(r));
    },

    /** DELETE /me/circles/{circle_id}/people/{person_id}: owner removes a person
     *  (`person_id` is `people[].id`). */
    removePerson(circleId: string, personId: string): Promise<void> {
      return coordinatorSessionRequest(
        'DELETE',
        `/me/circles/${encodeURIComponent(circleId)}/people/${encodeURIComponent(personId)}`,
      ).then((r) => fromSessionResult<void>(r));
    },
  },

  // ---- Identity (coordinator) ----------------------------------------------

  /** GET /me/identity - the calling member's own public key.
   *  Session-authenticated - see coordinatorAuth.ts. */
  getMyIdentity(): Promise<MyIdentityResponse> {
    return coordinatorSessionRequest('GET', '/me/identity').then((r) =>
      fromSessionResult<MyIdentityResponse>(r),
    );
  },

  // ---- Vault ---------------------------------------------------------------

  vault: {
    status(): Promise<VaultStatus> {
      return timedFetch(`${LOCAL_BASE}/vault/status`).then((r) =>
        handleResponse<VaultStatus>(r),
      );
    },

    rescan(): Promise<VaultRescanResult> {
      return timedFetch(`${LOCAL_BASE}/vault/rescan`, { method: 'POST' }).then((r) =>
        handleResponse<VaultRescanResult>(r),
      );
    },

    disconnect(): Promise<{ reaped_chunks: number }> {
      return timedFetch(`${LOCAL_BASE}/vault`, { method: 'DELETE' }).then((r) =>
        handleResponse<{ reaped_chunks: number }>(r),
      );
    },
  },
};
