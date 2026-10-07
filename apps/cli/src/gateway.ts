/**
 * Thin OpenAI-compatible client for the Potluck gateway (127.0.0.1).
 *
 * Uses native fetch + manual SSE parsing — no SDK, no tools/function-calling.
 * The gateway request schema is deliberately small: messages / model /
 * temperature / max_tokens / stream / stop. Everything the agent does is
 * prompt-based on top of plain chat completions.
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
}

interface ModelsResponse {
  data?: ModelInfo[];
}

interface ChatChunk {
  choices?: Array<{ delta?: { content?: string } }>;
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
  // A parallel gateway change adds local|mine routing; sending the header is
  // harmless (ignored) on a gateway that doesn't yet understand it.
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

  if (lock && (res.headers.get("x-potluck-model-lock") !== "1"
      || res.headers.get("x-potluck-model-sha256") !== lock.model.sha256)) {
    await res.body.cancel();
    throw new Error("Gateway did not acknowledge the exact model lock. No output was accepted; upgrade the runtime.");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

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
          if (data === "[DONE]") return;
          if (!data) continue;

          let chunk: ChatChunk;
          try {
            chunk = JSON.parse(data) as ChatChunk;
          } catch {
            if (opts.requireDone) throw new Error("Gateway returned an invalid stream frame.");
            continue; // skip a partial/garbled frame rather than crash
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
          const delta = chunk.choices?.[0]?.delta?.content;
          if (delta) yield delta;
        }
      }
    }
    if (opts.requireDone) throw new Error("Gateway stream ended before completion.");
  } finally {
    // Best-effort: release the stream if the consumer stops early.
    void reader.cancel().catch(() => {});
  }
}
