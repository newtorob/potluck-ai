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
}

interface ModelsResponse {
  data?: ModelInfo[];
}

interface ChatChunk {
  choices?: Array<{ delta?: { content?: string } }>;
  error?: { message?: string };
}

function authHeaders(cfg: GatewayConfig): Record<string, string> {
  return {
    "content-type": "application/json",
    authorization: `Bearer ${cfg.apiKey}`,
  };
}

async function bodyText(res: Response): Promise<string> {
  try {
    return (await res.text()).slice(0, 500);
  } catch {
    return "";
  }
}

/** GET /v1/models — installed + reachable models, in the gateway's order. */
export async function listModels(cfg: GatewayConfig): Promise<ModelInfo[]> {
  const res = await fetch(`${cfg.baseURL}/models`, {
    headers: authHeaders(cfg),
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
  const res = await fetch(`${cfg.baseURL}/chat/completions`, {
    method: "POST",
    headers: authHeaders(cfg),
    body: JSON.stringify({
      model: opts.model,
      messages,
      temperature: opts.temperature ?? 0.2,
      max_tokens: opts.maxTokens ?? 2048,
      stream: true,
      stop: opts.stop,
    }),
    signal: opts.signal,
  });

  if (!res.ok || !res.body) {
    throw new Error(
      `gateway POST /chat/completions failed: ${res.status} ${await bodyText(res)}`,
    );
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

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
            continue; // skip a partial/garbled frame rather than crash
          }
          if (chunk.error) {
            throw new Error(chunk.error.message ?? "gateway inference error");
          }
          const delta = chunk.choices?.[0]?.delta?.content;
          if (delta) yield delta;
        }
      }
    }
  } finally {
    // Best-effort: release the stream if the consumer stops early.
    void reader.cancel().catch(() => {});
  }
}
