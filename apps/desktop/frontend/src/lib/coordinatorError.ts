/**
 * coordinatorError - classifies a coordinator call's outcome into a
 * discriminated union of user-facing states.
 *
 * The coordinator's failure modes are not interchangeable. Collapsing every
 * one of them into "can't reach the coordinator" is wrong most of the time:
 *
 *   - 401 means the member's session expired or was never established -
 *     the fix is signing in again, not checking a network cable.
 *   - 403 means the coordinator's shared-secret token gate rejected the
 *     app itself. This should never happen for the desktop app in prod
 *     (that gate only applies to server-to-server calls) - if it recurs,
 *     it's a genuine coordinator misconfiguration and should read as one,
 *     not get silently misread as "signed out" or "offline."
 *   - status 0 (or a thrown network error) is the only case that's
 *     actually "can't reach the coordinator."
 *   - 5xx is the coordinator being up but broken.
 *
 * This module is the one place that draws the line, so every surface that
 * shows a coordinator failure can tell the user the truth about which one
 * it hit. Pure and dependency-free (aside from `detailOf`) so it's cheap to
 * unit test - see coordinatorError.test.ts.
 */
import { detailOf, type CoordinatorSessionResult } from '@/lib/coordinatorAuth';

export type CoordinatorErrorState =
  | { kind: 'ok' }
  | { kind: 'unauthenticated'; status: 401; detail: string }
  | { kind: 'forbidden'; status: 403; detail: string }
  | { kind: 'unreachable'; status: 0; detail: string }
  | { kind: 'server_error'; status: number; detail: string }
  /** Any other non-2xx (400, 404, 409, 429, …) - the call site usually
   *  knows more about what this means than this module does; `detail`
   *  carries the coordinator's own message for the caller to surface. */
  | { kind: 'error'; status: number; detail: string };

/**
 * `CoordinatorErrorState` minus `ok` - the shape every call site actually
 * stores in an "error" field, since a successful call clears the error
 * rather than setting it to `{ kind: 'ok' }`. `classifyCoordinatorError` is
 * only ever called from a `catch` block on a rejected coordinator call, so
 * it never legitimately produces `ok` - this type says so.
 */
export type CoordinatorFailureState = Exclude<CoordinatorErrorState, { kind: 'ok' }>;

function fromStatus(status: number, detail: string): CoordinatorErrorState {
  if (status >= 200 && status < 300) return { kind: 'ok' };
  if (status === 401) return { kind: 'unauthenticated', status, detail };
  if (status === 403) return { kind: 'forbidden', status, detail };
  if (status === 0) return { kind: 'unreachable', status, detail };
  if (status >= 500) return { kind: 'server_error', status, detail };
  return { kind: 'error', status, detail };
}

/** Classify a raw `coordinatorSessionRequest` result directly - use this
 *  when the caller has the `{status, body}` pair in hand rather than a
 *  thrown error (e.g. before it's been passed through `fromSessionResult`). */
export function classifyCoordinatorResult(res: CoordinatorSessionResult): CoordinatorErrorState {
  return fromStatus(res.status, detailOf(res.body, `HTTP ${res.status}`));
}

// localApi's `fromSessionResult` throws `Error('[localApi] {status}: {detail}')`
// on any non-2xx - this is the shape every localApi.circles/credits/network.*
// call that goes through coordinatorSessionRequest rejects with. Parsed back
// out here so call sites that already `catch` a thrown error (the normal
// async/await shape) don't have to route around it.
const LOCAL_API_ERROR_RE = /^\[localApi\] (\d+): ([\s\S]*)$/;

/** Classify a thrown error from a `localApi` coordinator call (circles,
 *  credits, network.myStats/myContributions*, getMyIdentity). Anything that
 *  isn't the `[localApi] {status}: …` shape (a raw TypeError from a failed
 *  `fetch`, an AbortError from a timeout, etc.) is treated as `unreachable`
 * - there's no HTTP status to classify, and those genuinely are transport
 *  failures. */
export function classifyCoordinatorError(err: unknown): CoordinatorFailureState {
  if (err instanceof Error) {
    const match = LOCAL_API_ERROR_RE.exec(err.message);
    const statusStr = match?.[1];
    const detail = match?.[2];
    if (statusStr !== undefined && detail !== undefined) {
      const state = fromStatus(Number(statusStr), detail);
      // fromSessionResult only throws on non-2xx, so this never actually
      // happens - the fallback keeps the return type honest either way.
      if (state.kind === 'ok') return { kind: 'error', status: Number(statusStr), detail };
      return state;
    }
    return { kind: 'unreachable', status: 0, detail: err.message };
  }
  return { kind: 'unreachable', status: 0, detail: 'unknown error' };
}

/**
 * Feature-neutral default copy for the two states that mean the same thing
 * everywhere they appear (unreachable / server_error / forbidden). `ok` and
 * `unauthenticated` are deliberately omitted - a successful state renders no
 * message, and "sign in" copy reads better written with the feature name in
 * it (see CirclesView), so call sites own that one directly.
 */
export function defaultCoordinatorErrorCopy(
  state: Exclude<CoordinatorFailureState, { kind: 'unauthenticated' }>,
): { title: string; description: string } {
  switch (state.kind) {
    case 'forbidden':
      return {
        title: "The coordinator rejected the app's credentials",
        description:
          "This is a coordinator configuration problem, not something wrong on your end. If you run this coordinator, check its token-gate settings.",
      };
    case 'unreachable':
      return {
        title: "Can't reach the coordinator",
        description: 'Check your connection, then try again.',
      };
    case 'server_error':
      return {
        title: 'The coordinator had an error',
        description: 'Try again shortly.',
      };
    case 'error':
      return {
        title: 'Something went wrong',
        description: state.detail,
      };
  }
}
