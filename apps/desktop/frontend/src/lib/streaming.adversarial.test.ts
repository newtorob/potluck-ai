/**
 * Adversarial review of streamTokens.
 *
 * Adversary model: the producer on the far end of the body is not trusted to
 * be well-behaved. It may be a buggy proxy that reframes the stream, a peer
 * that dies mid-JSON, or a contributor that puts hostile text in an error
 * envelope. The parser is the first thing that touches those bytes, so its
 * failure mode has to be "yield nothing / yield an error", never "yield a
 * plausible-looking success".
 *
 * These cases are deliberately disjoint from streaming.test.ts.
 */

import { describe, it, expect } from 'vitest';
import { streamTokens } from './streaming';
import type { StreamEvent } from './streaming';

const SSE_HEADERS = { 'Content-Type': 'text/event-stream' };

function sse(body: string): Response {
  return new Response(body, { status: 200, headers: SSE_HEADERS });
}

/** Body delivered as an explicit sequence of reads. */
function chunked(chunks: (string | Uint8Array)[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      for (const chunk of chunks) {
        c.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk);
      }
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

function text(events: StreamEvent[]): string {
  return events
    .filter((e): e is Extract<StreamEvent, { type: 'token' }> => e.type === 'token')
    .map((e) => e.content)
    .join('');
}

// ---------------------------------------------------------------------------
// 1. Framing
// ---------------------------------------------------------------------------

describe('framing', () => {
  it('DEFECT 1: parses a CRLF-framed stream (SSE spec allows CRLF)', async () => {
    const events = await collect(
      sse(
        'data: {"choices":[{"delta":{"content":"one "}}]}\r\n\r\n' +
          'data: {"choices":[{"delta":{"content":"two"}}]}\r\n\r\n' +
          'data: [DONE]\r\n\r\n',
      ),
    );

    expect(text(events)).toBe('one two');
    expect(events.some((e) => e.type === 'done')).toBe(true);
  });

  it('CRLF failure is at least closed: no DONE is fabricated', async () => {
    const events = await collect(
      sse(
        'data: {"choices":[{"delta":{"content":"one "}}]}\r\n\r\n' +
          'data: {"choices":[{"delta":{"content":"two"}}]}\r\n\r\n' +
          'data: [DONE]\r\n\r\n',
      ),
    );

    // Whatever the parser does with CRLF, it must never report a finished
    // stream it did not actually see terminate cleanly.
    if (text(events) !== 'one two') {
      expect(events.some((e) => e.type === 'done')).toBe(false);
    }
  });

  it('strips a UTF-8 BOM at the head of the stream', async () => {
    const events = await collect(
      sse('﻿data: {"choices":[{"delta":{"content":"bom ok"}}]}\n\ndata: [DONE]\n\n'),
    );

    expect(events).toEqual([{ type: 'token', content: 'bom ok' }, { type: 'done' }]);
  });

  it('accepts data: with no space and with two spaces', async () => {
    const events = await collect(
      sse(
        'data:{"choices":[{"delta":{"content":"tight"}}]}\n\n' +
          'data:  {"choices":[{"delta":{"content":" loose"}}]}\n\n' +
          'data: [DONE]\n\n',
      ),
    );

    expect(text(events)).toBe('tight loose');
  });

  it('accepts the sentinel with no space, trailing spaces, and a trailing CR', async () => {
    expect(await collect(sse('data:[DONE]\n\n'))).toEqual([{ type: 'done' }]);
    expect(await collect(sse('data: [DONE]   \n\n'))).toEqual([{ type: 'done' }]);
    expect(await collect(sse('data: [DONE]\r\n\n'))).toEqual([{ type: 'done' }]);
  });

  it('reassembles an event split in the middle of a multi-byte character', async () => {
    const encoder = new TextEncoder();
    const whole = encoder.encode('data: {"choices":[{"delta":{"content":"café — 🥟"}}]}\n\ndata: [DONE]\n\n');
    // Cut inside the 4-byte emoji sequence near the end of the payload.
    const cut = whole.length - 14;
    const events = await collect(chunked([whole.slice(0, cut), whole.slice(cut)]));

    expect(text(events)).toBe('café — 🥟');
    expect(events.some((e) => e.type === 'done')).toBe(true);
  });

  it('reassembles one event delivered across five reads', async () => {
    const events = await collect(
      chunked(['da', 'ta: {"choi', 'ces":[{"delta":{"cont', 'ent":"five"}}]}\n', '\ndata: [DONE]\n\n']),
    );

    expect(events).toEqual([{ type: 'token', content: 'five' }, { type: 'done' }]);
  });

  it('carries a 1 MB content delta through intact', async () => {
    const big = 'x'.repeat(1024 * 1024);
    const events = await collect(
      sse(`data: ${JSON.stringify({ choices: [{ delta: { content: big } }] })}\n\ndata: [DONE]\n\n`),
    );

    expect(text(events)).toHaveLength(1024 * 1024);
    expect(events.some((e) => e.type === 'done')).toBe(true);
  });

  it('ignores a data line that is only whitespace', async () => {
    const events = await collect(sse('data:   \n\ndata: {"choices":[{"delta":{"content":"after"}}]}\n\ndata: [DONE]\n\n'));

    expect(events).toEqual([{ type: 'token', content: 'after' }, { type: 'done' }]);
  });

  it('ignores a comment-only block and a non-JSON data payload', async () => {
    const events = await collect(
      sse(': ping\n\ndata: ping\n\ndata: {"choices":[{"delta":{"content":"alive"}}]}\n\ndata: [DONE]\n\n'),
    );

    expect(events).toEqual([{ type: 'token', content: 'alive' }, { type: 'done' }]);
  });

  it('yields nothing after the DONE sentinel, even in the same read', async () => {
    const events = await collect(
      sse(
        'data: {"choices":[{"delta":{"content":"real"}}]}\n\n' +
          'data: [DONE]\n\n' +
          'data: {"choices":[{"delta":{"content":" INJECTED"}}]}\n\n' +
          'data: {"error":{"category":"oom","message":"too late"}}\n\n',
      ),
    );

    expect(events).toEqual([{ type: 'token', content: 'real' }, { type: 'done' }]);
  });

  it('does not crash on a partial JSON fragment left in the buffer at close', async () => {
    const events = await collect(
      sse('data: {"choices":[{"delta":{"content":"good"}}]}\n\ndata: {"choices":[{"del'),
    );

    expect(events).toEqual([{ type: 'token', content: 'good' }]);
    expect(events.some((e) => e.type === 'done')).toBe(false);
  });

  it('does not crash when the body ends mid multi-byte character', async () => {
    const encoder = new TextEncoder();
    const whole = encoder.encode('data: {"choices":[{"delta":{"content":"tail 🥟"}}]}\n\n');
    const events = await collect(chunked([whole.slice(0, whole.length - 6)]));

    // Truncated JSON: nothing usable, but no throw and no fabricated done.
    expect(events.some((e) => e.type === 'done')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 2. Error shapes
// ---------------------------------------------------------------------------

describe('hostile error envelopes', () => {
  async function firstError(body: string) {
    const events = await collect(sse(body));
    const err = events.find((e) => e.type === 'error');
    return err as Extract<StreamEvent, { type: 'error' }> | undefined;
  }

  it('treats an empty-string error as a failure with fallback text', async () => {
    const err = await firstError('data: {"error":""}\n\ndata: [DONE]\n\n');
    expect(err?.type).toBe('error');
    expect(typeof err?.message).toBe('string');
    expect(err?.message.length).toBeGreaterThan(0);
    expect(err?.category).toBeUndefined();
  });

  it('treats a null, numeric, boolean or array error as a failure', async () => {
    for (const shape of ['null', '500', 'true', '["a","b"]']) {
      const err = await firstError(`data: {"error":${shape}}\n\ndata: [DONE]\n\n`);
      expect(err, `shape ${shape} must still be an error`).toBeDefined();
      expect(typeof err?.message).toBe('string');
      expect(err?.message.length).toBeGreaterThan(0);
    }
  });

  it('keeps the category when the message field is not a string', async () => {
    const err = await firstError('data: {"error":{"category":"oom","message":{"nested":true}}}\n\n');
    expect(err?.category).toBe('oom');
    expect(typeof err?.message).toBe('string');
  });

  it('drops a non-string category instead of coercing it', async () => {
    const err = await firstError('data: {"error":{"category":123,"message":"raw peer text"}}\n\n');
    // Documented behaviour, and the seed of DEFECT 2 in the store test:
    // losing the category downgrades this to the "trusted machine" branch.
    expect(err?.category).toBeUndefined();
    expect(err?.message).toBe('raw peer text');
  });

  it('passes an unknown category through untouched', async () => {
    const err = await firstError('data: {"error":{"category":"quantum_flux","message":"m"}}\n\n');
    expect(err?.category).toBe('quantum_flux');
  });

  it('passes a prototype-key category through untouched', async () => {
    for (const key of ['__proto__', 'constructor', 'toString']) {
      const err = await firstError(
        `data: {"error":{"category":${JSON.stringify(key)},"message":"m"}}\n\n`,
      );
      expect(err?.category).toBe(key);
    }
  });

  it('lets the error win over content carried in the same chunk', async () => {
    const events = await collect(
      sse(
        'data: {"choices":[{"delta":{"content":"kept"}}]}\n\n' +
          'data: {"error":"boom","choices":[{"delta":{"content":" DROPPED"}}]}\n\n' +
          'data: [DONE]\n\n',
      ),
    );

    expect(events.map((e) => e.type)).toEqual(['token', 'error', 'done']);
    expect(text(events)).toBe('kept');
  });

  it('does not let an error message re-frame the stream (SSE injection)', async () => {
    const payload = { error: 'boom\n\ndata: [DONE]\n\ndata: {"choices":[{"delta":{"content":"ghost"}}]}' };
    const events = await collect(sse(`data: ${JSON.stringify(payload)}\n\n`));

    expect(events).toHaveLength(1);
    expect(events[0]?.type).toBe('error');
    expect(text(events)).toBe('');
    expect(events.some((e) => e.type === 'done')).toBe(false);
  });

  it('carries markdown and HTML in an error message through as inert text', async () => {
    const hostile = '<img src=x onerror=alert(1)> [click](https://evil.example)';
    const err = await firstError(`data: ${JSON.stringify({ error: hostile })}\n\n`);
    expect(err?.message).toBe(hostile);
  });

  it('ignores served_scope values outside the closed set', async () => {
    const events = await collect(
      sse('data: {"meta":{"served_by":"contributor","served_scope":"household-of-bob"}}\n\ndata: [DONE]\n\n'),
    );
    const meta = events.find((e) => e.type === 'meta') as Extract<StreamEvent, { type: 'meta' }>;
    expect(meta.served_scope).toBeUndefined();
  });

  it('rejects a meta envelope whose served_by is not in the closed set', async () => {
    const events = await collect(
      sse('data: {"meta":{"served_by":"peer-7.example","output_tokens":9}}\n\ndata: [DONE]\n\n'),
    );
    expect(events.some((e) => e.type === 'meta')).toBe(false);
  });
});
