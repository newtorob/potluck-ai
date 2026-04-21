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
  content: string;
}

/**
 * Parse the ReadableStream from a streaming chat response into tokens.
 *
 * @param response — raw fetch Response with Content-Type: text/event-stream
 * @yields {StreamToken} one content chunk per SSE event
 * @throws if the response body is null or an SSE payload cannot be parsed
 */
export async function* streamTokens(response: Response): AsyncGenerator<StreamToken> {
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

        const content = extractContent(parsed);
        if (content) {
          yield { content };
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
