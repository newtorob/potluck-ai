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

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status: 'ok';
  model_loaded: boolean;
  active_model_id: string | null;
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

export interface PotluckSettings {
  runtime: RuntimeSettings;
  privacy: PrivacySettings;
  network: NetworkSettings;
  app: AppSettingsShape;
  models_path: string;
  active_model_id: string | null;
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
};
