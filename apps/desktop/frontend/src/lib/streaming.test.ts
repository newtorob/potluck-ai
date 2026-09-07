/**
 * streamTokens must surface everything the wire can say, not just tokens.
 *
 * Three producers feed this parser and they disagree about how a stream ends:
 *   - local sidecar / household peer: {"error": "<string>"} then data: [DONE]
 *   - coordinator peer failure:       {"error": {category, message}} then close
 *   - coordinator timeout:            error, then a meta envelope, then [DONE]
 *   - any of them:                    body just ends (abort, crash, drop)
 *
 * The consumer can only tell "finished" from "cut off" if this parser reports
 * the [DONE] sentinel as its own event and stays silent when the reader closes
 * without one.
 */

import { describe, it, expect } from 'vitest';
import { streamTokens } from './streaming';
import type { StreamEvent } from './streaming';

const SSE_HEADERS = { 'Content-Type': 'text/event-stream' };

/** Whole body available up front (one read). */
function sse(body: string): Response {
  return new Response(body, { status: 200, headers: SSE_HEADERS });
}

/** Body delivered as an explicit sequence of reads, so a test can split an
 *  event anywhere it likes (mid-JSON, mid-frame). */
function chunked(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      for (const chunk of chunks) c.enqueue(encoder.encode(chunk));
      c.close();
    },
  });
  return new Response(body, { status: 200, headers: SSE_HEADERS });
}

async function collect(response: Response): Promise<StreamEvent[]> {
  const events: StreamEvent[] = [];
  for await (const event of streamTokens(response)) events.push(event);
  return events;
}

describe('streamTokens', () => {
  it('yields a string error from the local sidecar, then the DONE sentinel', async () => {
    const events = await collect(
      sse(
        'data: {"choices":[{"delta":{"content":"half "}}]}\n\n' +
          'data: {"error":"model crashed"}\n\n' +
          'data: [DONE]\n\n',
      ),
    );

    expect(events).toEqual([
      { type: 'token', content: 'half ' },
      { type: 'error', message: 'model crashed' },
      { type: 'done' },
    ]);
  });

  it('yields a coordinator category error and never a done when the stream just closes', async () => {
    const events = await collect(
      sse(
        'data: {"choices":[{"delta":{"content":"half "}}]}\n\n' +
          'data: {"error":{"category":"peer_disconnected","message":"peer went away"}}\n\n',
      ),
    );

    expect(events).toEqual([
      { type: 'token', content: 'half ' },
      { type: 'error', category: 'peer_disconnected', message: 'peer went away' },
    ]);
    expect(events.some((e) => e.type === 'done')).toBe(false);
  });

  it('reports the timeout shape error before the trailing meta and done', async () => {
    // Producer bug (audit finding 5): the coordinator's timeout branch emits an
    // error and then still finishes the envelope. The parser reports all three
    // in order; the consumer decides the error wins.
    const events = await collect(
      sse(
        'data: {"error":{"category":"timeout","message":"contributor timed out"}}\n\n' +
          'data: {"meta":{"served_by":"contributor","served_scope":"pool","finish_reason":"stop"}}\n\n' +
          'data: [DONE]\n\n',
      ),
    );

    expect(events.map((e) => e.type)).toEqual(['error', 'meta', 'done']);
    expect(events[0]).toEqual({
      type: 'error',
      category: 'timeout',
      message: 'contributor timed out',
    });
  });

  it('emits no done event when the reader closes without the sentinel', async () => {
    const events = await collect(sse('data: {"choices":[{"delta":{"content":"cut off"}}]}\n\n'));

    expect(events).toEqual([{ type: 'token', content: 'cut off' }]);
  });

  it('emits a done event for a bare sentinel', async () => {
    expect(await collect(sse('data: [DONE]\n\n'))).toEqual([{ type: 'done' }]);
  });

  it('reassembles an event split across two reads mid-JSON', async () => {
    const events = await collect(
      chunked(['data: {"choices":[{"delta":{"con', 'tent":"rejoined"}}]}\n\n']),
    );

    expect(events).toEqual([{ type: 'token', content: 'rejoined' }]);
  });

  it('ignores event, id, retry and comment lines around the data line', async () => {
    const events = await collect(
      sse(': keep-alive\nevent: message\nid: 7\nretry: 500\ndata: {"choices":[{"delta":{"content":"ok"}}]}\n\n'),
    );

    expect(events).toEqual([{ type: 'token', content: 'ok' }]);
  });

  it('joins multiple data lines in one event with a newline', async () => {
    const events = await collect(
      sse('data: {"choices":[{"delta":\ndata: {"content":"joined"}}]}\n\n'),
    );

    expect(events).toEqual([{ type: 'token', content: 'joined' }]);
  });

  it('flushes a trailing event that arrived without a terminating blank line', async () => {
    const events = await collect(
      sse('data: {"choices":[{"delta":{"content":"a"}}]}\n\ndata: [DONE]'),
    );

    expect(events).toEqual([{ type: 'token', content: 'a' }, { type: 'done' }]);
  });

  it('skips unparsable data and keeps reading', async () => {
    const events = await collect(
      sse(
        'data: {not json at all\n\n' +
          'data: {"choices":[{"delta":{"content":"survived"}}]}\n\n' +
          'data: [DONE]\n\n',
      ),
    );

    expect(events).toEqual([{ type: 'token', content: 'survived' }, { type: 'done' }]);
  });

  it('preserves the circle routing scope on the meta envelope', async () => {
    const events = await collect(
      sse(
        'data: {"meta":{"served_by":"contributor","served_scope":"circle","output_tokens":12,"duration_ms":900,"finish_reason":"stop"}}\n\n' +
          'data: [DONE]\n\n',
      ),
    );

    expect(events[0]).toEqual({
      type: 'meta',
      served_by: 'contributor',
      served_scope: 'circle',
      output_tokens: 12,
      duration_ms: 900,
      finish_reason: 'stop',
    });
  });

  it('falls back to a fixed message for an error envelope with no readable text', async () => {
    const events = await collect(sse('data: {"error":{}}\n\n'));

    expect(events).toHaveLength(1);
    const first = events[0];
    expect(first?.type).toBe('error');
    expect(first?.type === 'error' && first.message.length > 0).toBe(true);
  });

  it('reads a detail field when the error envelope carries no message', async () => {
    const events = await collect(sse('data: {"error":{"detail":"queue full"}}\n\n'));

    expect(events).toEqual([{ type: 'error', message: 'queue full' }]);
  });

  it('drops empty content deltas', async () => {
    const events = await collect(
      sse('data: {"choices":[{"delta":{"content":""}}]}\n\ndata: [DONE]\n\n'),
    );

    expect(events).toEqual([{ type: 'done' }]);
  });

  it('cancels the reader when the consumer stops early', async () => {
    let cancelled = false;
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"one"}}]}\n\n'));
        c.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"two"}}]}\n\n'));
      },
      cancel() {
        cancelled = true;
      },
    });
    const response = new Response(body, { status: 200, headers: SSE_HEADERS });

    for await (const event of streamTokens(response)) {
      expect(event).toEqual({ type: 'token', content: 'one' });
      break;
    }

    expect(cancelled).toBe(true);
  });
});
