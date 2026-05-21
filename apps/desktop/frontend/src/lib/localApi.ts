/**
 * HTTP client for the Python sidecar running at 127.0.0.1:8321.
 *
 * ALL inference and model management goes through this client.
 * Prompts never leave the machine — this API is strictly local.
 *
 * Base URL is configurable via VITE_LOCAL_API_URL for testing,
 * defaulting to the sidecar's hard-coded address.
 *
 * See guide §4.5 for the full contract.
 */

import type { ChatMessage, ChatOptions } from '@/types/chat';
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
import type { AgentSession, MemoryPreview, SessionDetail, ToolCall } from '@/types/agents';
import { coordinatorBase } from '@/stores/connectionStore';

const LOCAL_BASE =
  (import.meta.env['VITE_LOCAL_API_URL'] as string | undefined) ?? 'http://127.0.0.1:8321';

// Coordinator URL — the Phase G control plane (separate from the local
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
        ? String((body as { detail: unknown }).detail)
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
        ? String((body as { detail: unknown }).detail)
        : res.statusText;
    throw new Error(`[localApi] ${res.status}: ${msg}`);
  }
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status: 'ok';
  model_loaded: boolean;
  active_model_id: string | null;
  /** Context window actually loaded into the runtime. Null when no model is loaded. */
  n_ctx_loaded?: number | null;
  /** Big Model Mode: list of llama.cpp RPC endpoints the current model
   *  is distributed across. Null when loaded locally or no model loaded. */
  loaded_rpc_servers?: string[] | null;
}

// ---------------------------------------------------------------------------
// Settings shapes (mirror Python PotluckSettings)
// ---------------------------------------------------------------------------

export interface RuntimeSettings {
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
  contributor_schedule: Record<string, unknown> | null;
  max_gpu_percent: number;
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
  models_path: string;
  active_model_id: string | null;
  active_project: string | null;
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
  served_by: 'contributor' | 'local' | null;
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

// ---- Credit ledger (Phase G m4) ------------------------------------------

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
 *  invariant: NO peer_id / hostname / hardware fields here, by design.
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

export interface MyJobStats {
  jobs_24h: number;
  tokens_received_24h: number;
  avg_duration_ms: number;
}

// ---------------------------------------------------------------------------
// API surface
// ---------------------------------------------------------------------------

export const localApi = {
  /** GET /health — liveness check; also tells us if a model is loaded */
  health(): Promise<HealthResponse> {
    return fetch(`${LOCAL_BASE}/health`).then((r) => handleResponse<HealthResponse>(r));
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
    return fetch(`${LOCAL_BASE}/chat/completions`, init);
  },

  /**
   * POST /jobs/stream against the coordinator (NOT the local sidecar).
   * Returns a Response whose body is an OpenAI-compatible SSE stream
   * — same shape /chat/completions emits, so streamTokens() consumes
   *   it without any modification.
   *
   * v0 of "Use contributor network": single-turn dispatch. The full
   * conversation history is NOT sent — only the latest prompt — because
   * the coordinator's JobAssignedMessage has no multi-turn slot today
   * and bolting one on without a thoughtful design is a privacy hazard.
   * Multi-turn over the network is m4+ work.
   */
  streamNetworkJob(
    prompt: string,
    options: { modelSlug: string; temperature?: number; maxTokens?: number; abortSignal?: AbortSignal }
  ): Promise<Response> {
    const init: RequestInit = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt,
        model_slug: options.modelSlug,
        sampling: {
          temperature: options.temperature ?? 0.7,
          max_tokens: options.maxTokens ?? 2048,
        },
      }),
    };
    if (options.abortSignal) init.signal = options.abortSignal;
    return fetch(`${coordinatorBase()}/jobs/stream`, init);
  },

  // ---- Models --------------------------------------------------------------

  listModels(): Promise<Model[]> {
    return fetch(`${LOCAL_BASE}/models`).then((r) => handleResponse<Model[]>(r));
  },

  installModel(id: string): Promise<{ status: string }> {
    return fetch(`${LOCAL_BASE}/models/${encodeURIComponent(id)}/install`, {
      method: 'POST',
    }).then((r) => handleResponse<{ status: string }>(r));
  },

  deleteModel(id: string): Promise<{ status: string }> {
    return fetch(`${LOCAL_BASE}/models/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }).then((r) => handleResponse<{ status: string }>(r));
  },

  loadModel(
    id: string,
    options: { rpc_servers?: string[] } = {},
  ): Promise<{ model_path: string; load_time_seconds: number; rpc_servers?: string[] | null }> {
    // When `rpc_servers` is set, this triggers Big Model Mode — the
    // sidecar reloads the model with remote llama.cpp RPC backends
    // participating as ggml devices. Requires the Potluck-patched
    // llama-cpp-python wheel; the sidecar surfaces a 501 if not.
    const body =
      options.rpc_servers && options.rpc_servers.length > 0
        ? { rpc_servers: options.rpc_servers }
        : null;
    return fetch(`${LOCAL_BASE}/models/${encodeURIComponent(id)}/load`, {
      method: 'POST',
      ...(body
        ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
        : {}),
    }).then((r) =>
      handleResponse<{
        model_path: string;
        load_time_seconds: number;
        rpc_servers?: string[] | null;
      }>(r),
    );
  },

  downloadProgress(id: string): Promise<DownloadProgress> {
    return fetch(`${LOCAL_BASE}/models/${encodeURIComponent(id)}/download-progress`).then((r) =>
      handleResponse<DownloadProgress>(r),
    );
  },

  // ---- Hardware ------------------------------------------------------------

  detectHardware(): Promise<Record<string, unknown>> {
    return fetch(`${LOCAL_BASE}/hardware`).then((r) =>
      handleResponse<Record<string, unknown>>(r),
    );
  },

  // ---- Settings ------------------------------------------------------------

  getSettings(): Promise<PotluckSettings> {
    return fetch(`${LOCAL_BASE}/settings`).then((r) => handleResponse<PotluckSettings>(r));
  },

  updateSettings(patch: PotluckSettingsPatch): Promise<PotluckSettings> {
    return fetch(`${LOCAL_BASE}/settings`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).then((r) => handleResponse<PotluckSettings>(r));
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
      return fetch(`${LOCAL_BASE}/memory${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<Memory[]>(r),
      );
    },

    projects(): Promise<ProjectSummary[]> {
      return fetch(`${LOCAL_BASE}/memory/projects`).then((r) =>
        handleResponse<ProjectSummary[]>(r),
      );
    },

    get(id: string): Promise<Memory> {
      return fetch(`${LOCAL_BASE}/memory/${encodeURIComponent(id)}`).then((r) =>
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
      return fetch(`${LOCAL_BASE}/memory`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<Memory>(r));
    },

    update(
      id: string,
      patch: { content?: string; category?: MemoryCategory; confidence?: number; project?: string | null },
    ): Promise<Memory> {
      return fetch(`${LOCAL_BASE}/memory/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }).then((r) => handleResponse<Memory>(r));
    },

    delete(id: string): Promise<void> {
      return fetch(`${LOCAL_BASE}/memory/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      }).then((r) => handle204(r));
    },

    search(body: {
      query?: string;
      current_messages?: ChatMessage[];
      max_memories?: number;
      min_confidence?: number;
      project?: string | null;
      semantic?: boolean;
    }): Promise<Memory[]> {
      return fetch(`${LOCAL_BASE}/memory/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<Memory[]>(r));
    },

    related(id: string, limit = 8): Promise<RelatedMemory[]> {
      const params = new URLSearchParams({ limit: String(limit) });
      return fetch(
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
      return fetch(`${LOCAL_BASE}/memory/keywords${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<KeywordFrequency[]>(r),
      );
    },

    extract(body: { messages: ChatMessage[]; conversation_id: string }): Promise<Memory[]> {
      return fetch(`${LOCAL_BASE}/memory/extract`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<Memory[]>(r));
    },

    wipe(): Promise<MemoryWipeResult> {
      return fetch(`${LOCAL_BASE}/memory/wipe`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true }),
      }).then((r) => handleResponse<MemoryWipeResult>(r));
    },

    export(): Promise<MemoryExportResult> {
      return fetch(`${LOCAL_BASE}/memory/export`).then((r) =>
        handleResponse<MemoryExportResult>(r),
      );
    },

    import(body: { memories: Memory[] }): Promise<MemoryImportResult> {
      return fetch(`${LOCAL_BASE}/memory/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<MemoryImportResult>(r));
    },

    clusters(opts: { project?: string } = {}): Promise<ClusterResponse> {
      const params = new URLSearchParams();
      if (opts.project !== undefined && opts.project !== '') params.set('project', opts.project);
      const qs = params.toString();
      return fetch(`${LOCAL_BASE}/memory/clusters${qs ? `?${qs}` : ''}`).then((r) =>
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
      return fetch(`${LOCAL_BASE}/tasks${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<TaskState[]>(r),
      );
    },

    get(id: string): Promise<TaskState> {
      return fetch(`${LOCAL_BASE}/tasks/${encodeURIComponent(id)}`).then((r) =>
        handleResponse<TaskState>(r),
      );
    },

    create(body: TaskCreateInput): Promise<TaskState> {
      return fetch(`${LOCAL_BASE}/tasks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<TaskState>(r));
    },

    update(id: string, patch: TaskUpdateInput): Promise<TaskState> {
      return fetch(`${LOCAL_BASE}/tasks/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }).then((r) => handleResponse<TaskState>(r));
    },

    completeStep(id: string, step: string): Promise<TaskState> {
      return fetch(`${LOCAL_BASE}/tasks/${encodeURIComponent(id)}/complete-step`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ step }),
      }).then((r) => handleResponse<TaskState>(r));
    },

    addError(id: string, body: { error: string; resolution?: string }): Promise<TaskState> {
      return fetch(`${LOCAL_BASE}/tasks/${encodeURIComponent(id)}/add-error`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<TaskState>(r));
    },

    delete(id: string): Promise<void> {
      return fetch(`${LOCAL_BASE}/tasks/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      }).then((r) => handle204(r));
    },
  },

  // ---- Agents --------------------------------------------------------------

  agents: {
    listSessions(opts: { include_disconnected?: boolean; limit?: number } = {}): Promise<AgentSession[]> {
      const params = new URLSearchParams();
      if (opts.include_disconnected !== undefined) {
        params.set('include_disconnected', opts.include_disconnected ? 'true' : 'false');
      }
      if (opts.limit !== undefined) params.set('limit', String(opts.limit));
      const qs = params.toString();
      return fetch(`${LOCAL_BASE}/agents/sessions${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<AgentSession[]>(r),
      );
    },

    getSession(id: string): Promise<SessionDetail> {
      return fetch(`${LOCAL_BASE}/agents/sessions/${encodeURIComponent(id)}`).then((r) =>
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
      return fetch(`${LOCAL_BASE}/agents/activity${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<ToolCall[]>(r),
      );
    },

    deleteSession(id: string): Promise<void> {
      return fetch(`${LOCAL_BASE}/agents/sessions/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      }).then((r) => handle204(r));
    },

    clearDisconnected(): Promise<{ removed: number }> {
      return fetch(`${LOCAL_BASE}/agents/sessions`, { method: 'DELETE' }).then((r) =>
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
      return fetch(`${LOCAL_BASE}/agents/sessions/sweep`, { method: 'POST' }).then((r) =>
        handleResponse<{ marked_disconnected: number; deleted_worthless: number }>(r),
      );
    },

    /** Resolve a batch of memory ids to lightweight previews for the
     *  debugger's memory-overlay panel. Capped server-side at 100 ids per
     *  call. Missing ids are silently dropped — the panel renders that as
     *  "memory deleted since this call". */
    memoryPreviews(ids: string[]): Promise<MemoryPreview[]> {
      if (ids.length === 0) return Promise.resolve([]);
      return fetch(`${LOCAL_BASE}/agents/memory-previews`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      }).then((r) => handleResponse<MemoryPreview[]>(r));
    },

    /** Download a portable JSON trace of one session — session metadata +
     *  full tool-call history + memory previews for any ids touched.
     *  Returns the response as a Blob (not decoded JSON) so the caller can
     *  trigger a file save while preserving the server's filename hint.
     *  See the /trace endpoint for the privacy guarantees. */
    async exportSessionTrace(
      sessionId: string,
    ): Promise<{ blob: Blob; filename: string }> {
      const res = await fetch(
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

  // ---- Chat history (sidecar SQLite, local-only) ---------------------------

  history: {
    list(opts: { project?: string | null; include_all?: boolean; limit?: number } = {}): Promise<PersistedConversation[]> {
      const params = new URLSearchParams();
      if (opts.include_all !== undefined) params.set('include_all', opts.include_all ? 'true' : 'false');
      if (opts.project !== undefined && opts.project !== null) params.set('project', opts.project);
      if (opts.limit !== undefined) params.set('limit', String(opts.limit));
      const qs = params.toString();
      return fetch(`${LOCAL_BASE}/chat/conversations${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<PersistedConversation[]>(r),
      );
    },

    get(id: string): Promise<PersistedConversationDetail> {
      return fetch(`${LOCAL_BASE}/chat/conversations/${encodeURIComponent(id)}`).then((r) =>
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
      return fetch(`${LOCAL_BASE}/chat/conversations`, {
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
      return fetch(`${LOCAL_BASE}/chat/conversations/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }).then((r) => handleResponse<PersistedConversation>(r));
    },

    delete(id: string): Promise<void> {
      return fetch(`${LOCAL_BASE}/chat/conversations/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      }).then((r) => handle204(r));
    },

    appendMessage(conversationId: string, body: {
      id?: string;
      role: 'user' | 'assistant' | 'system';
      content: string;
      status?: 'complete' | 'streaming' | 'partial' | 'error';
      served_by?: 'contributor' | 'local' | null;
      output_tokens?: number | null;
      duration_ms?: number | null;
      finish_reason?: string | null;
    }): Promise<PersistedMessage> {
      return fetch(`${LOCAL_BASE}/chat/conversations/${encodeURIComponent(conversationId)}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((r) => handleResponse<PersistedMessage>(r));
    },

    updateMessage(messageId: string, patch: {
      content?: string;
      status?: 'complete' | 'streaming' | 'partial' | 'error';
      served_by?: 'contributor' | 'local' | null;
      output_tokens?: number | null;
      duration_ms?: number | null;
      finish_reason?: string | null;
    }): Promise<{ id: string; updated: boolean }> {
      return fetch(`${LOCAL_BASE}/chat/messages/${encodeURIComponent(messageId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }).then((r) => handleResponse<{ id: string; updated: boolean }>(r));
    },

    clearMessages(conversationId: string): Promise<{ removed: number }> {
      return fetch(`${LOCAL_BASE}/chat/conversations/${encodeURIComponent(conversationId)}/messages`, {
        method: 'DELETE',
      }).then((r) => handleResponse<{ removed: number }>(r));
    },
  },

  // ---- Network (coordinator-side fleet + per-member stats) ---------------

  network: {
    /** GET /transparency/fleet on the coordinator. Aggregate-only — no
     *  peer_id / hostname / individual peer fields, by manifesto rule. */
    fleet(): Promise<FleetSummary> {
      return fetch(`${coordinatorBase()}/transparency/fleet`).then((r) =>
        handleResponse<FleetSummary>(r),
      );
    },

    /** GET /health on the coordinator. Used as a cheap reachability probe
     *  to gate UI affordances that depend on the coordinator (e.g. the
     *  chat composer's Network-mode toggle). Resolves to `true` only when
     *  the response is a 2xx; any other outcome (5xx, network error,
     *  timeout) resolves to `false` rather than throwing — callers should
     *  not need a try/catch for this. */
    health(timeoutMs = 2000): Promise<boolean> {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      return fetch(`${coordinatorBase()}/health`, { signal: controller.signal })
        .then((r) => r.ok)
        .catch(() => false)
        .finally(() => clearTimeout(timer));
    },

    /** GET /jobs/stats on the coordinator. Returns this member's
     *  requester-side counts + average latency for the last 24h. */
    myStats(): Promise<MyJobStats> {
      return fetch(`${coordinatorBase()}/jobs/stats`).then((r) =>
        handleResponse<MyJobStats>(r),
      );
    },
  },

  // ---- Credits (coordinator — Phase G m4 ledger) -------------------------

  credits: {
    /** GET /me/credit-balance — aggregated earned / spent / net plus
     *  per-model breakdown. Decimal fields arrive as strings; keep them
     *  that way — parse/format only at the render layer. */
    balance(): Promise<CreditBalance> {
      return fetch(`${coordinatorBase()}/me/credit-balance`).then((r) =>
        handleResponse<CreditBalance>(r),
      );
    },

    /** GET /me/credit-history — newest-first ledger entries.
     *  `direction` filters to 'earned' or 'spent'; omit for all.
     *  `limit` defaults to 50 server-side; pass 10 for the glance widget. */
    history(opts: { direction?: 'earned' | 'spent'; limit?: number } = {}): Promise<CreditHistory> {
      const params = new URLSearchParams();
      if (opts.direction !== undefined) params.set('direction', opts.direction);
      if (opts.limit !== undefined) params.set('limit', String(opts.limit));
      const qs = params.toString();
      return fetch(`${coordinatorBase()}/me/credit-history${qs ? `?${qs}` : ''}`).then((r) =>
        handleResponse<CreditHistory>(r),
      );
    },
  },

  // ---- Vault ---------------------------------------------------------------

  vault: {
    status(): Promise<VaultStatus> {
      return fetch(`${LOCAL_BASE}/vault/status`).then((r) =>
        handleResponse<VaultStatus>(r),
      );
    },

    rescan(): Promise<VaultRescanResult> {
      return fetch(`${LOCAL_BASE}/vault/rescan`, { method: 'POST' }).then((r) =>
        handleResponse<VaultRescanResult>(r),
      );
    },

    disconnect(): Promise<{ reaped_chunks: number }> {
      return fetch(`${LOCAL_BASE}/vault`, { method: 'DELETE' }).then((r) =>
        handleResponse<{ reaped_chunks: number }>(r),
      );
    },
  },
};
