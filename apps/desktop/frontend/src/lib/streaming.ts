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
   *  own machines, "circle" = a trusted-circle member, "pool" = an open-pool
   *  peer. Tier label only, never identity. */
  served_scope?: 'mine' | 'circle' | 'pool';
  output_tokens?: number;
  duration_ms?: number;
  finish_reason?: string;
}

/** A failure the producer reported mid-body. HTTP status is already 200 by
 *  then, so this envelope is the only signal that the reply is not what the
 *  user asked for.
 *
 *  Two shapes on the wire: the local sidecar and household peers send
 *  `{"error": "<string>"}`; the coordinator sends
 *  `{"error": {"category": "...", "message": "..."}}`. `category` is present
 *  only for the coordinator, and it is what the consumer should render from,
 *  since `message` on that path is contributor-supplied text. */
export interface StreamError {
  type: 'error';
  message: string;
  category?: string;
}

/** The `data: [DONE]` sentinel. Its absence is meaningful: a body that just
 *  ends (abort, generator exception, dropped connection) yields no done event,
 *  which is how the consumer tells a finished reply from a truncated one. */
export interface StreamDone {
  type: 'done';
}

export type StreamEvent = StreamToken | StreamMeta | StreamError | StreamDone;

/** Used when an error envelope carries no readable text of its own. */
const UNSPECIFIED_STREAM_ERROR = 'The stream reported an error with no details.';

/** One parsed SSE event block: whatever it yielded, plus whether it was the
 *  `[DONE]` sentinel (which ends the stream). */
interface ParsedBlock {
  events: StreamEvent[];
  done: boolean;
}

/**
 * Turn one SSE event block into events.
 *
 * Per the SSE spec, a block is a set of `field: value` lines: `data:` lines
 * accumulate and are joined with newlines, `event:` / `id:` / `retry:` are
 * ignored here, and a line starting with `:` is a keep-alive comment. Only one
 * leading space after the colon is part of the delimiter, not the value.
 */
function parseEventBlock(block: string): ParsedBlock {
  const dataLines: string[] = [];

  for (const rawLine of block.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
    if (line === '') continue;
    if (line.startsWith(':')) continue; // comment / keep-alive

    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    if (field !== 'data') continue; // event:, id:, retry:, anything unknown

    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    dataLines.push(value);
  }

  if (dataLines.length === 0) return { events: [], done: false };

  const data = dataLines.join('\n');
  if (data.trim() === '[DONE]') return { events: [{ type: 'done' }], done: true };

  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    // Malformed event — skip silently.
    return { events: [], done: false };
  }

  // Errors are probed first: an error envelope must never be mistaken for a
  // token or meta chunk, and it is the only thing on the wire that says the
  // reply is not what was asked for.
  const error = extractError(parsed);
  if (error) return { events: [error], done: false };

  const meta = extractMeta(parsed);
  if (meta) return { events: [meta], done: false };

  const content = extractContent(parsed);
  if (content) return { events: [{ type: 'token', content }], done: false };

  return { events: [], done: false };
}

/**
 * Parse the ReadableStream from a streaming chat response into events.
 *
 * All three producers (local sidecar, household peer, coordinator) speak the
 * same OpenAI delta shape for content. They differ at the edges:
 *   - the coordinator adds a `meta` envelope before `[DONE]`
 *   - all of them can emit an `error` envelope mid-body, with HTTP already 200
 *   - any of them can simply stop sending, with no `[DONE]` at all
 *
 * The generator reports all three. It does not decide what they mean: an error
 * is yielded and reading continues, so the consumer chooses when to stop.
 *
 * @param response — raw fetch Response with Content-Type: text/event-stream
 * @yields {StreamEvent} discriminated union of token, meta, error and done
 * @throws if the response body is null
 */
export interface StreamReadOptions {
  signal?: AbortSignal;
  firstTokenTimeoutMs?: number;
  idleTimeoutMs?: number;
  /** Absolute performance.now() deadline shared by all attempts. */
  deadline?: number;
}

export async function* streamTokens(
  response: Response, options: StreamReadOptions = {},
): AsyncGenerator<StreamEvent> {
  if (!response.body) {
    throw new Error('[streaming] Response body is null');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let progressDeadline = performance.now() + (options.firstTokenTimeoutMs ?? Infinity);

  async function read(): Promise<ReadableStreamReadResult<Uint8Array>> {
    const deadline = Math.min(progressDeadline, options.deadline ?? Infinity);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let onAbort: (() => void) | undefined;
    try {
      return await new Promise((resolve, reject) => {
        const fail = (reason: unknown) => {
          reject(reason);
          void reader.cancel().catch(() => undefined);
        };
        onAbort = () => fail(options.signal?.reason ?? new DOMException('Stopped', 'AbortError'));
        if (options.signal?.aborted) { onAbort(); return; }
        options.signal?.addEventListener('abort', onAbort, { once: true });
        if (Number.isFinite(deadline)) {
          timer = setTimeout(() => fail(new Error('The computer stopped responding before the reply finished.')),
            Math.max(0, deadline - performance.now()));
        }
        void reader.read().then(resolve, reject);
      });
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      if (onAbort) options.signal?.removeEventListener('abort', onAbort);
    }
  }

  try {
    while (true) {
      const { done, value } = await read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      // The SSE spec allows CRLF, LF or a bare CR as the line break, and a
      // proxy can reframe a stream from one to the other. Normalise before
      // splitting, or a CRLF-framed body never splits at all and arrives as
      // one unparseable block at close. A lone trailing "\r" stays in the
      // buffer and pairs with the next chunk's "\n" on the following round.
      buffer = buffer.replace(/\r\n/g, '\n');

      // SSE events are separated by double newlines
      const blocks = buffer.split('\n\n');
      // Keep the last incomplete chunk in the buffer
      buffer = blocks.pop() ?? '';

      for (const block of blocks) {
        const { events, done: sawDone } = parseEventBlock(block);
        for (const event of events) {
          if (event.type === 'token') {
            progressDeadline = performance.now() + (options.idleTimeoutMs ?? Infinity);
          }
          yield event;
        }
        if (sawDone) return;
      }
    }

    // The body ended. Flush any multi-byte remainder out of the decoder and
    // treat whatever is left in the buffer as a final event block: a producer
    // that closes right after its last event never sends the trailing blank
    // line, and that event is still real.
    buffer += decoder.decode();
    buffer = buffer.replace(/\r\n/g, '\n');
    if (buffer.trim() !== '') {
      const { events } = parseEventBlock(buffer);
      for (const event of events) yield event;
    }
  } finally {
    // Cancel before releasing: an early `break` in the consumer should drop the
    // connection rather than leave the producer streaming into a lock we hold.
    try {
      await reader.cancel();
    } catch {
      /* already closed or errored — nothing to release the peer from */
    }
    try {
      reader.releaseLock();
    } catch {
      /* lock already gone */
    }
  }
}

/** Safely extracts the delta content string from an OpenAI-format SSE chunk */
function extractContent(parsed: unknown): string | null {
  if (
    typeof parsed !== 'object' ||
    parsed === null ||
    !('choices' in parsed) ||
    !Array.isArray((parsed).choices)
  ) {
    return null;
  }

  const chunk = parsed as { choices: { delta?: { content?: unknown } }[] };
  const first = chunk.choices[0];
  if (!first) return null;

  const content = first.delta?.content;
  return typeof content === 'string' ? content : null;
}

/** Pull a readable string field off a record, or null. */
function stringField(source: Record<string, unknown>, key: string): string | null {
  const value = source[key];
  return typeof value === 'string' && value !== '' ? value : null;
}

/** Recognise a mid-stream error envelope in any of the shapes the three
 *  producers emit. Returns null for any chunk without an `error` key.
 *
 *    {"error": "model crashed"}                        local sidecar, household
 *    {"error": {"category": "oom", "message": "..."}}  coordinator
 *
 *  Anything else carrying an `error` key still counts as a failure: we take
 *  whatever text it offers and fall back to a fixed message rather than
 *  dropping the event, because a dropped error reads to the user as a
 *  successful (but short) answer. */
function extractError(parsed: unknown): StreamError | null {
  if (typeof parsed !== 'object' || parsed === null || !('error' in parsed)) {
    return null;
  }

  const err = (parsed as { error: unknown }).error;
  if (typeof err === 'string') {
    return { type: 'error', message: err === '' ? UNSPECIFIED_STREAM_ERROR : err };
  }

  // Structured error: read from the error object when there is one, otherwise
  // from the envelope itself ({"error": true, "message": "..."}).
  const source: Record<string, unknown> =
    typeof err === 'object' && err !== null
      ? (err as Record<string, unknown>)
      : (parsed as Record<string, unknown>);

  const result: StreamError = {
    type: 'error',
    message:
      stringField(source, 'message') ?? stringField(source, 'detail') ?? UNSPECIFIED_STREAM_ERROR,
  };
  const category = stringField(source, 'category');
  if (category !== null) result.category = category;
  return result;
}

/** Recognise the coordinator's per-job meta envelope. Returns null for
 *  any chunk that doesn't carry a `meta` key (token deltas, error
 *  envelopes, anything else). */
function extractMeta(parsed: unknown): StreamMeta | null {
  if (
    typeof parsed !== 'object' ||
    parsed === null ||
    !('meta' in parsed) ||
    typeof (parsed).meta !== 'object' ||
    (parsed).meta === null
  ) {
    return null;
  }
  const m = (parsed as { meta: Record<string, unknown> }).meta;
  const served = m['served_by'];
  if (served !== 'contributor' && served !== 'local') return null;
  const result: StreamMeta = { type: 'meta', served_by: served };
  const scope = m['served_scope'];
  if (scope === 'mine' || scope === 'circle' || scope === 'pool') result.served_scope = scope;
  if (typeof m['output_tokens'] === 'number') result.output_tokens = m['output_tokens'];
  if (typeof m['duration_ms'] === 'number') result.duration_ms = m['duration_ms'];
  if (typeof m['finish_reason'] === 'string') result.finish_reason = m['finish_reason'];
  return result;
}
