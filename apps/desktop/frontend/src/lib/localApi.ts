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
import type { AgentSession, SessionDetail, ToolCall } from '@/types/agents';

const LOCAL_BASE =
  (import.meta.env['VITE_LOCAL_API_URL'] as string | undefined) ?? 'http://127.0.0.1:8321';

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
    return fetch(`${LOCAL_BASE}/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: messages.map(({ role, content }) => ({ role, content })),
        stream: true,
        temperature: options.temperature ?? 0.7,
        max_tokens: options.maxTokens ?? 2048,
      }),
      signal: options.abortSignal,
    });
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

  loadModel(id: string): Promise<{ model_path: string; load_time_seconds: number }> {
    return fetch(`${LOCAL_BASE}/models/${encodeURIComponent(id)}/load`, {
      method: 'POST',
    }).then((r) => handleResponse<{ model_path: string; load_time_seconds: number }>(r));
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

  updateSettings(patch: Partial<PotluckSettings>): Promise<PotluckSettings> {
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
