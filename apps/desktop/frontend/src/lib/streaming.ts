/**
 * SSE streaming parser for the local inference API.
 *
 * The Python sidecar emits OpenAI-compatible server-sent events:
 *
 *   data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}
 *   data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}
 *   data: [DONE]
 *
 * `streamTokens` is an async generator that consumes a fetch Response body
 * and yields content strings one token at a time. Callers can iterate with
 * `for await` and append each token to the UI.
 */

export interface StreamToken {
  type: 'token';
  content: string;
}

/** Server-sent metadata about the completed job. The coordinator's
 *  /jobs/stream endpoint emits one of these just before [DONE]; the
 *  local sidecar's /chat/completions never does. Frontend consumers
 *  pattern-match on `type` to handle both shapes. */
export interface StreamMeta {
  type: 'meta';
  served_by: 'contributor' | 'local';
  /** Routing tier the coordinator served from: "mine" = one of the requester's
   *  own machines, "pool" = an open-pool peer. Tier label only, never identity. */
  served_scope?: 'mine' | 'pool';
  output_tokens?: number;
  duration_ms?: number;
  finish_reason?: string;
}

export type StreamEvent = StreamToken | StreamMeta;

/**
 * Parse the ReadableStream from a streaming chat response into events.
 *
 * Backwards-compatible: the local sidecar emits only `token` events
 * (OpenAI delta-content shape); the coordinator's /jobs/stream emits
 * the same plus a final `meta` event right before `[DONE]`.
 *
 * @param response — raw fetch Response with Content-Type: text/event-stream
 * @yields {StreamEvent} discriminated union of token + meta events
 * @throws if the response body is null
 */
export async function* streamTokens(response: Response): AsyncGenerator<StreamEvent> {
  if (!response.body) {
    throw new Error('[streaming] Response body is null');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE events are separated by double newlines
      const events = buffer.split('\n\n');
      // Keep the last incomplete chunk in the buffer
      buffer = events.pop() ?? '';

      for (const event of events) {
        const line = event.trim();
        if (!line.startsWith('data:')) continue;

        const data = line.slice('data:'.length).trim();
        if (data === '[DONE]') return;

        let parsed: unknown;
        try {
          parsed = JSON.parse(data);
        } catch {
          // Malformed event — skip silently; real error handling in useChat
          continue;
        }

        const meta = extractMeta(parsed);
        if (meta) {
          yield meta;
          continue;
        }

        const content = extractContent(parsed);
        if (content) {
          yield { type: 'token', content };
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** Safely extracts the delta content string from an OpenAI-format SSE chunk */
function extractContent(parsed: unknown): string | null {
  if (
    typeof parsed !== 'object' ||
    parsed === null ||
    !('choices' in parsed) ||
    !Array.isArray((parsed as { choices: unknown }).choices)
  ) {
    return null;
  }

  const chunk = parsed as { choices: { delta?: { content?: unknown } }[] };
  const first = chunk.choices[0];
  if (!first) return null;

  const content = first.delta?.content;
  return typeof content === 'string' ? content : null;
}

/** Recognise the coordinator's per-job meta envelope. Returns null for
 *  any chunk that doesn't carry a `meta` key (token deltas, error
 *  envelopes, anything else). */
function extractMeta(parsed: unknown): StreamMeta | null {
  if (
    typeof parsed !== 'object' ||
    parsed === null ||
    !('meta' in parsed) ||
    typeof (parsed as { meta: unknown }).meta !== 'object' ||
    (parsed as { meta: unknown }).meta === null
  ) {
    return null;
  }
  const m = (parsed as { meta: Record<string, unknown> }).meta;
  const served = m['served_by'];
  if (served !== 'contributor' && served !== 'local') return null;
  const result: StreamMeta = { type: 'meta', served_by: served };
  const scope = m['served_scope'];
  if (scope === 'mine' || scope === 'pool') result.served_scope = scope;
  if (typeof m['output_tokens'] === 'number') result.output_tokens = m['output_tokens'];
  if (typeof m['duration_ms'] === 'number') result.duration_ms = m['duration_ms'];
  if (typeof m['finish_reason'] === 'string') result.finish_reason = m['finish_reason'];
  return result;
}
