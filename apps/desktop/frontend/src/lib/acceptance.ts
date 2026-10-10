/**
 * The engine's answer check, as the app shows it.
 *
 * After a model loads, the engine looks at the answers it gives on this setup,
 * falls back to the CPU when the GPU is what gives wrong answers, and remembers
 * the result. GET /health/inference carries that result under `acceptance`.
 * The engine sends the sentence for this machine's own user in `message`; the
 * app shows that text and writes no wording of its own for those two cases.
 *
 * An engine from before the check has no `acceptance` key. That reads as
 * "nothing to show", never as an error.
 */

export type AcceptanceStatus = 'passed' | 'failed' | 'running' | 'not_run' | 'skipped';
export type AcceptanceVerdict = 'ok' | 'gpu_wrong_output' | 'model_failed';

/** The fields the app uses, in the engine's own names. */
export interface Acceptance {
  status: AcceptanceStatus;
  model_id: string | null;
  verdict: AcceptanceVerdict | null;
  /** Set when the check moved the model from the GPU to the CPU. */
  fallback: { from: string | null; to: string | null; reason: string | null } | null;
  /** One plain sentence from the engine, or null. */
  message: string | null;
}

const STATUSES: ReadonlySet<string> = new Set([
  'passed',
  'failed',
  'running',
  'not_run',
  'skipped',
]);
const VERDICTS: ReadonlySet<string> = new Set(['ok', 'gpu_wrong_output', 'model_failed']);
const MAX_MESSAGE = 300;

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

/**
 * Engine text that is shown to the person: one bounded line without escape
 * sequences or control characters. Null when nothing is left.
 */
export function cleanEngineText(value: unknown, max = MAX_MESSAGE): string | null {
  if (typeof value !== 'string') return null;
  const text = value
    // eslint-disable-next-line no-control-regex -- the point is to remove terminal escape sequences
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, '')
    // eslint-disable-next-line no-control-regex
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\p{Cc}/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, max);
  return text === '' ? null : text;
}

/** The `acceptance` object, or null when it is missing or not something this app understands. */
export function parseAcceptance(raw: unknown): Acceptance | null {
  if (!isRecord(raw)) return null;
  const status = raw['status'];
  if (typeof status !== 'string' || !STATUSES.has(status)) return null;
  const verdict = raw['verdict'];
  const fallback = raw['fallback'];
  return {
    status: status as AcceptanceStatus,
    model_id: cleanEngineText(raw['model_id'], 200),
    verdict:
      typeof verdict === 'string' && VERDICTS.has(verdict) ? (verdict as AcceptanceVerdict) : null,
    fallback: isRecord(fallback)
      ? {
          from: cleanEngineText(fallback['from'], 24),
          to: cleanEngineText(fallback['to'], 24),
          reason: cleanEngineText(fallback['reason'], 40),
        }
      : null,
    message: cleanEngineText(raw['message']),
  };
}

/** Read `acceptance` out of a GET /health/inference body. */
export function acceptanceFromHealth(body: unknown): Acceptance | null {
  return isRecord(body) ? parseAcceptance(body['acceptance']) : null;
}

/**
 * The banner shows when the check moved the model to the CPU or the model
 * failed it, and only when the engine said so in words: the app has none of
 * its own for these cases.
 */
export function needsBanner(a: Acceptance | null): a is Acceptance {
  return a !== null && (a.fallback !== null || a.status === 'failed') && a.message !== null;
}

/**
 * Identifies the state a dismissal applies to. A different model or a different
 * verdict gives a different key, so the banner comes back.
 */
export function bannerKey(a: Acceptance): string {
  return [a.model_id ?? '', a.status, a.verdict ?? '', a.fallback !== null ? 'fallback' : ''].join(
    '|',
  );
}

/** True when the check moved the loaded model to the CPU. */
export function movedToCpu(a: Acceptance | null, loadedSlug: string | null): a is Acceptance {
  return (
    a !== null &&
    a.fallback !== null &&
    loadedSlug !== null &&
    (a.model_id === null || a.model_id === loadedSlug)
  );
}
