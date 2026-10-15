/**
 * Thin OpenAI-compatible client for the Potluck gateway (127.0.0.1).
 *
 * Uses native fetch + manual SSE parsing. The built-in coding agent uses
 * text actions on top of chat completions; the gateway also supports native
 * tools for other clients.
 *
 * File contents only ever travel to this base URL (the local gateway).
 */

import { pinnedModel } from "./model-lock.js";
import type { GatewayConfig } from "./config.js";

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface ModelInfo {
  id: string;
  owned_by?: string;
}

export interface StreamOptions {
  model: string;
  temperature?: number;
  maxTokens?: number;
  stop?: string[];
  signal?: AbortSignal;
  /** Routing scope sent as the X-Potluck-Scope header (e.g. "local" | "mine"). */
  scope?: string;
  /** Trusted circle (name or ID) sent as X-Potluck-Circle with scope "circle". */
  circle?: string;
  /** Require the terminal SSE marker before accepting a response as complete. */
  requireDone?: boolean;
  /** Require a successful text generation (finish_reason: stop) and the terminal SSE marker before accepting coding actions. */
  requireSuccessfulFinish?: boolean;
  /** Synthetic readiness probes may deliberately exhaust a tiny output budget. Never use for coding actions. */
  allowLengthFinish?: boolean;
  /** Sees the response headers once the gateway accepted the request. Throwing stops the stream before any text is read. */
  onHeaders?: (headers: Headers) => void;
}

interface ModelsResponse {
  data?: ModelInfo[];
}

interface ChatChunk {
  choices?: Array<{
    index?: number;
    delta?: { content?: string | null; reasoning_content?: string | null; role?: string; tool_calls?: unknown; function_call?: unknown };
    finish_reason?: string | null;
  }>;
  potluck?: { finish_reason_inferred?: boolean };
  error?: { message?: string; code?: string; status?: number };
}

function authHeaders(cfg: GatewayConfig): Record<string, string> {
  return {
    "content-type": "application/json",
    authorization: `Bearer ${cfg.apiKey}`,
  };
}

/** The gateway answered a chat request with an error instead of a stream. */
export class GatewayRefusedError extends Error {
  /** The gateway's own one-line reason, when it gave one as plain text. */
  readonly detail: string | null;
  constructor(readonly status: number, body: string) {
    super(`gateway POST /chat/completions failed: ${status} ${body}`);
    let detail: unknown = null;
    try { detail = (JSON.parse(body) as { detail?: unknown }).detail; } catch { /* not JSON */ }
    this.detail = typeof detail === "string" && detail.length <= 400 ? detail : null;
  }
}

/** A context lookup failed; callers can explain the status without exposing the response body. */
export class GatewayContextError extends Error {
  constructor(readonly status: number, body: string) {
    super(`Could not check the model's context: ${status} ${body}`);
  }
}

async function bodyText(res: Response): Promise<string> {
  try {
    return (await res.text()).slice(0, 500);
  } catch {
    return "";
  }
}

/** GET /v1/models — installed + reachable models, in the gateway's order. */
export async function listModels(cfg: GatewayConfig, signal?: AbortSignal): Promise<ModelInfo[]> {
  const res = await fetch(`${cfg.baseURL}/models`, {
    headers: authHeaders(cfg),
    signal,
    redirect: "error",
  });
  if (!res.ok) {
    throw new Error(`gateway GET /models failed: ${res.status} ${await bodyText(res)}`);
  }
  const body = (await res.json()) as ModelsResponse;
  return body.data ?? [];
}

/** Loaded context on the selected route; undefined on an older gateway.
 * No project content is sent, and errors never broaden the requested scope. */
export async function modelContextWindow(
  cfg: GatewayConfig,
  model: string,
  scope: string,
  signal?: AbortSignal,
): Promise<number | undefined> {
  if (scope !== "local" && scope !== "mine") throw new Error("Coding context checks require local or mine scope.");
  pinnedModel(cfg.modelLock, model);
  const query = new URLSearchParams({ model, scope });
  if (cfg.modelLock) query.set("potluck_model_sha256", cfg.modelLock.model.sha256);
  const res = await fetch(`${cfg.baseURL}/potluck/context?${query}`, {
    headers: authHeaders(cfg), redirect: "error",
    signal: AbortSignal.any([AbortSignal.timeout(12_000), ...(signal ? [signal] : [])]),
  });
  if (res.status === 404) {
    const body = await bodyText(res);
    let detail = body;
    try { detail = (JSON.parse(body) as { detail?: string }).detail ?? body; } catch { /* plain legacy response */ }
    if (typeof detail === "string" && /^not found\.?$/i.test(detail.trim())) return undefined;
    throw new GatewayContextError(404, body);
  }
  if (res.status === 405) return undefined;
  if (!res.ok) throw new GatewayContextError(res.status, await bodyText(res));
  const body = await res.json() as { model?: unknown; scope?: unknown; context_window?: unknown };
  if (body.model !== model || body.scope !== scope) throw new Error("Gateway returned context information for a different model or scope.");
  const window = body.context_window;
  return typeof window === "number" && Number.isSafeInteger(window) && window > 0 ? window : undefined;
}

/**
 * POST /v1/chat/completions (stream=true). Yields content deltas as they arrive.
 * Aborts cleanly when `signal` fires (Ctrl-C cancels the current turn).
 */
export async function* streamChat(
  cfg: GatewayConfig,
  messages: ChatMessage[],
  opts: StreamOptions,
): AsyncGenerator<string, void, void> {
  const headers = authHeaders(cfg);
  const lock = cfg.modelLock;
  if (lock) {
    pinnedModel(lock, opts.model);
    if (!["local", "mine"].includes(opts.scope || "")) throw new Error("Project model locks require local or mine scope.");
    // Check before sending project content to an older runtime that might ignore the lock.
    const caps = await fetch(`${cfg.baseURL}/potluck/capabilities`, {
      headers, redirect: "error", signal: AbortSignal.any([AbortSignal.timeout(5000), ...(opts.signal ? [opts.signal] : [])]),
    });
    if (!caps.ok || (await caps.json() as { model_lock_version?: number }).model_lock_version !== 1) {
      throw new Error("Gateway does not support project model locks. Upgrade the Potluck runtime before using this project.");
    }
  }
  // Keep the caller's explicit routing choice on every request.
  if (opts.scope) headers["x-potluck-scope"] = opts.scope;
  if (opts.scope === "circle" && opts.circle) headers["x-potluck-circle"] = opts.circle;

  const res = await fetch(`${cfg.baseURL}/chat/completions`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: opts.model,
      ...(lock ? { potluck_model_sha256: lock.model.sha256 } : {}),
      messages,
      temperature: opts.temperature ?? 0.2,
      // 2048 truncated any non-trivial write/edit mid-JSON (the model's output
      // is code embedded in a JSON string, which is verbose). 4096 fits a real
      // file write without blowing a small model's context on the input side.
      max_tokens: opts.maxTokens ?? 4096,
      stream: true,
      stop: opts.stop,
    }),
    signal: opts.signal,
    redirect: "error",
  });

  if (!res.ok || !res.body) {
    const body = await bodyText(res);
    throw new GatewayRefusedError(res.status, body);
  }

  if (opts.onHeaders) {
    try { opts.onHeaders(res.headers); } catch (error) { await res.body.cancel(); throw error; }
  }
  if (lock && (res.headers.get("x-potluck-model-lock") !== "1"
      || res.headers.get("x-potluck-model-sha256") !== lock.model.sha256)) {
    await res.body.cancel();
    throw new Error("Gateway did not acknowledge the exact model lock. No output was accepted; upgrade the runtime.");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finishReason: string | undefined;
  let finishError: string | undefined;

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      if (buffer.length > 8 * 1024 * 1024) throw new Error("Gateway stream frame is too large.");

      let sep: number;
      // SSE frames are separated by a blank line.
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);

        for (const rawLine of frame.split("\n")) {
          const line = rawLine.trimStart();
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (data === "[DONE]") {
            if (opts.requireSuccessfulFinish) {
              if (finishError) throw new Error(finishError);
              if (finishReason !== "stop" && !(opts.allowLengthFinish && finishReason === "length")) {
                throw new Error("Gateway did not confirm a successful generation (missing finish_reason: stop).");
              }
            }
            return;
          }
          if (!data) continue;

          let chunk: ChatChunk;
          try {
            chunk = JSON.parse(data) as ChatChunk;
          } catch {
            if (opts.requireDone || opts.requireSuccessfulFinish) throw new Error("Gateway returned an invalid stream frame.");
            continue; // skip a partial/garbled frame rather than crash
          }
          if (opts.requireSuccessfulFinish && (!chunk || typeof chunk !== "object" || Array.isArray(chunk))) {
            throw new Error("Gateway returned an invalid stream frame.");
          }
          if (chunk.error) {
            // The gateway turned the request away before any answer started
            // (no machine offering the model, an expired sign-in). That is a
            // refusal with a reason to show, not a failed generation.
            if (chunk.error.code === "refused" && typeof chunk.error.message === "string") {
              throw new GatewayRefusedError(Number(chunk.error.status) || 503, JSON.stringify({ detail: chunk.error.message }));
            }
            throw new Error(chunk.error.message ?? "gateway inference error");
          }
          if (opts.requireSuccessfulFinish) {
            if (chunk.potluck?.finish_reason_inferred === true) {
              finishError ??= "The remote machine did not report how the generation finished. Update Potluck on the serving machine and retry.";
            }
            if (!Array.isArray(chunk.choices)) throw new Error("Gateway returned an invalid completion chunk.");
            // Usage-only chunks may follow the terminal choice. We request one
            // completion, so another choice cannot establish its success.
            if (chunk.choices.length === 0) continue;
            const choice = chunk.choices[0];
            if (chunk.choices.length !== 1 || !choice || typeof choice !== "object" || Array.isArray(choice)
                || (choice.index !== undefined && choice.index !== 0)) {
              throw new Error("Gateway returned an invalid completion choice.");
            }
            const delta = choice.delta;
            if (delta != null && (typeof delta !== "object" || Array.isArray(delta))) {
              throw new Error("Gateway returned an invalid completion delta.");
            }
            if ((delta?.content != null && typeof delta.content !== "string")
                || (delta?.reasoning_content != null && typeof delta.reasoning_content !== "string")) {
              throw new Error("Gateway returned an invalid completion content delta.");
            }
            const reason = choice.finish_reason;
            if (reason != null && (typeof reason !== "string" || !reason)) {
              throw new Error("Gateway returned an invalid finish_reason.");
            }
            const nativeTool = delta?.tool_calls != null || delta?.function_call != null;
            if (finishReason !== undefined && (delta?.content || delta?.reasoning_content || delta?.role || nativeTool || reason != null)) {
              finishError ??= "Gateway returned output after the generation had finished.";
            }
            if (nativeTool) finishError ??= "Gateway returned native tool calls instead of a completed text response.";
            if (reason != null) {
              finishReason ??= reason;
              if (reason === "length") {
                if (!opts.allowLengthFinish) finishError ??= "The model hit its output limit, so this reply was not applied. Try a smaller change or a larger loaded context.";
              } else if (reason !== "stop") {
                finishError ??= `Gateway generation ended with finish_reason: ${reason}; the reply was not accepted as complete.`;
              }
            }
            // Drain through [DONE] even after an unsuccessful finish so the
            // runtime can clean up normally. Callers must consume the whole
            // stream before treating any displayed text as an executable action.
          }
          const delta = chunk.choices?.[0]?.delta?.content;
          if (delta) yield delta;
        }
      }
    }
    if (opts.requireDone || opts.requireSuccessfulFinish) throw new Error("Gateway stream ended before completion.");
  } finally {
    // Best-effort: release the stream if the consumer stops early.
    void reader.cancel().catch(() => {});
  }
}
