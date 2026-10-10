import { describe, expect, it } from 'vitest';
import {
  acceptanceFromHealth,
  bannerKey,
  cleanEngineText,
  movedToCpu,
  needsBanner,
  parseAcceptance,
  type Acceptance,
} from './acceptance';

const FALLBACK_MESSAGE =
  'The GPU gave wrong answers for this model, so it is running on the CPU, which is slower. Updating the graphics driver usually fixes this.';
const FAILED_MESSAGE =
  'This model did not pass its answer check on this machine. You can still use it yourself. It is not offered to other people.';

/** The object the contract spells out, with overrides. */
function raw(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: 1,
    status: 'passed',
    model_id: 'qwen3-8b-q4',
    level: 'quick',
    checked_at: '2026-10-09T14:32:05Z',
    from_proof: false,
    ran_on: { device_kind: 'gpu', backend: 'cuda', device_name: 'RTX 2080 Ti', driver: '560.35' },
    checks: [{ id: 'arithmetic', passed: true, required: true }],
    verdict: 'ok',
    fallback: null,
    message: null,
    ...over,
  };
}
const fellBack = (over: Record<string, unknown> = {}) =>
  raw({
    verdict: 'gpu_wrong_output',
    fallback: { from: 'gpu', to: 'cpu', reason: 'gpu_wrong_output' },
    message: FALLBACK_MESSAGE,
    ...over,
  });
const failed = (over: Record<string, unknown> = {}) =>
  raw({ status: 'failed', verdict: 'model_failed', message: FAILED_MESSAGE, ...over });

describe('parseAcceptance', () => {
  it('keeps the fields the app uses and nothing else', () => {
    const parsed = parseAcceptance(
      fellBack({ secret: 'never-show', ran_on: { driver: 'never-show' } }),
    );
    expect(parsed).toEqual({
      status: 'passed',
      model_id: 'qwen3-8b-q4',
      verdict: 'gpu_wrong_output',
      fallback: { from: 'gpu', to: 'cpu', reason: 'gpu_wrong_output' },
      message: FALLBACK_MESSAGE,
    });
    expect(JSON.stringify(parsed)).not.toContain('never-show');
  });

  it.each([undefined, null, 'passed', 3, [], {}, { status: 'mystery' }, { status: 7 }])(
    'reads %j as no acceptance',
    (value) => {
      expect(parseAcceptance(value)).toBeNull();
    },
  );

  it('reads a response with no acceptance key (an older engine), or no body, as nothing', () => {
    expect(
      acceptanceFromHealth({ build: {}, devices: [], loaded: null, benchmark: null }),
    ).toBeNull();
    expect(acceptanceFromHealth(null)).toBeNull();
    expect(acceptanceFromHealth('nope')).toBeNull();
    expect(acceptanceFromHealth([])).toBeNull();
    expect(acceptanceFromHealth({ acceptance: raw() })?.status).toBe('passed');
  });

  it('treats unknown verdicts and malformed fallbacks as absent instead of failing', () => {
    expect(parseAcceptance(raw({ verdict: 'weird' }))?.verdict).toBeNull();
    expect(parseAcceptance(raw({ fallback: 'yes' }))?.fallback).toBeNull();
    expect(parseAcceptance(raw({ fallback: [] }))?.fallback).toBeNull();
    expect(parseAcceptance(raw({ model_id: 12 }))?.model_id).toBeNull();
  });
});

describe('cleanEngineText', () => {
  it('strips escape sequences and control characters and collapses white space', () => {
    expect(cleanEngineText('\u001b[31mNo good\u001b[0m\u0007\n  at all\u0000')).toBe(
      'No good at all',
    );
    expect(cleanEngineText('\u001b]0;title\u0007text')).toBe('text');
  });

  it('is null for anything that is not text or has no text left', () => {
    expect(cleanEngineText(5)).toBeNull();
    expect(cleanEngineText(null)).toBeNull();
    expect(cleanEngineText('  \n\t ')).toBeNull();
  });

  it('bounds a long sentence', () => {
    expect(cleanEngineText('x'.repeat(5000))).toHaveLength(300);
    expect(parseAcceptance(failed({ message: 'y'.repeat(5000) }))?.message).toHaveLength(300);
  });

  it('keeps the engine sentence exactly as sent', () => {
    expect(parseAcceptance(failed())?.message).toBe(FAILED_MESSAGE);
    expect(parseAcceptance(fellBack())?.message).toBe(FALLBACK_MESSAGE);
  });
});

describe('needsBanner', () => {
  const of = (over: Record<string, unknown>) => parseAcceptance(raw(over));

  it('is true for a fallback to the CPU and for a failed model', () => {
    expect(needsBanner(parseAcceptance(fellBack()))).toBe(true);
    expect(needsBanner(parseAcceptance(failed()))).toBe(true);
  });

  it('is true for a fallback even when the run on the CPU passed', () => {
    expect(parseAcceptance(fellBack())?.status).toBe('passed');
    expect(needsBanner(parseAcceptance(fellBack({ status: 'failed' })))).toBe(true);
  });

  it.each(['passed', 'running', 'skipped', 'not_run'])('is false for %s', (status) => {
    expect(
      needsBanner(of({ status, message: status === 'passed' ? null : 'whatever the engine says' })),
    ).toBe(false);
  });

  it('is false without a sentence from the engine, because the app writes none of its own', () => {
    expect(needsBanner(parseAcceptance(failed({ message: null })))).toBe(false);
    expect(needsBanner(parseAcceptance(fellBack({ message: '   ' })))).toBe(false);
  });

  it('is false for an engine without the check', () => {
    expect(needsBanner(null)).toBe(false);
    expect(needsBanner(acceptanceFromHealth({}))).toBe(false);
  });
});

describe('bannerKey', () => {
  const key = (a: Acceptance | null) => (a === null ? '' : bannerKey(a));

  it('stays the same for the same model and verdict', () => {
    expect(key(parseAcceptance(failed()))).toBe(
      key(parseAcceptance(failed({ checked_at: '2026-10-10T00:00:00Z' }))),
    );
  });

  it('changes with the model, the verdict, or a fallback', () => {
    const base = key(parseAcceptance(failed()));
    expect(key(parseAcceptance(failed({ model_id: 'other' })))).not.toBe(base);
    expect(key(parseAcceptance(fellBack({ model_id: 'qwen3-8b-q4' })))).not.toBe(base);
    expect(key(parseAcceptance(failed({ verdict: 'gpu_wrong_output' })))).not.toBe(base);
  });
});

describe('movedToCpu', () => {
  it('is true for the loaded model after a fallback', () => {
    expect(movedToCpu(parseAcceptance(fellBack()), 'qwen3-8b-q4')).toBe(true);
  });

  it('is false for another loaded model, no loaded model, no fallback, or no result', () => {
    expect(movedToCpu(parseAcceptance(fellBack()), 'other')).toBe(false);
    expect(movedToCpu(parseAcceptance(fellBack()), null)).toBe(false);
    expect(movedToCpu(parseAcceptance(fellBack({ model_id: null })), null)).toBe(false);
    expect(movedToCpu(parseAcceptance(failed()), 'qwen3-8b-q4')).toBe(false);
    expect(movedToCpu(parseAcceptance(raw()), 'qwen3-8b-q4')).toBe(false);
    expect(movedToCpu(null, 'qwen3-8b-q4')).toBe(false);
  });

  it('counts a result that names no model as being about the loaded one', () => {
    expect(movedToCpu(parseAcceptance(fellBack({ model_id: null })), 'qwen3-8b-q4')).toBe(true);
  });
});
