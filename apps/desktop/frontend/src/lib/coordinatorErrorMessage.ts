/**
 * Turns a failed household-setup / pairing call into UI copy, upgrading a
 * cryptic auth rejection into an actionable "switch coordinators and retry"
 * message whenever the app's configured coordinator disagrees with what
 * this machine's mesh daemon is actually connected to (see
 * connectionStore.isCoordinatorMismatched - the proactive version of this
 * same fix is CoordinatorMismatchBanner, shown before the user even tries).
 *
 * Two failure shapes both mean "the coordinator didn't recognize this
 * request," and both get the same treatment here:
 *
 *   - `CoordinatorError` with httpStatus 401 - the session-authed call
 *     (issuePairingToken, used by "Set up household" and "Add a machine")
 *     was rejected directly by the app's configured coordinator.
 *   - `MeshApiError` with code 'coordinator_error' whose message reads
 *     "unauthorized" - the mesh DAEMON's own upstream call to redeem a
 *     pairing token was rejected (apps/desktop/go/potluck-mesh's /pair
 *     handler forwards the coordinator's rejection body verbatim). This is
 *     the exact shape a stale override produces:
 *     `coordinator_error: {"detail":"unauthorized"}`.
 *
 * Every other error shape falls through to the same messages the call
 * sites already produced before this existed - this file doesn't change
 * behavior for anything that isn't an auth rejection.
 */

import { CoordinatorError, MeshApiError } from '@/lib/meshClient';
import { isCoordinatorMismatched } from '@/stores/connectionStore';

export interface CoordinatorAuthErrorInfo {
  message: string;
  /** Set only when there's a coordinator mismatch and this looks like an
   *  auth rejection - callers should offer a "switch and retry" action that
   *  calls setCoordinatorUrl(switchToUrl) and re-runs the failed action. */
  switchToUrl?: string;
}

export function describeCoordinatorAuthError(
  err: unknown,
  opts: { coordinatorUrl: string; sidecarReportedUrl: string | null },
): CoordinatorAuthErrorInfo {
  const isSessionAuthRejection = err instanceof CoordinatorError && err.httpStatus === 401;
  const isDaemonRelayedRejection =
    err instanceof MeshApiError &&
    err.code === 'coordinator_error' &&
    /unauthorized/i.test(err.message);

  if (isSessionAuthRejection || isDaemonRelayedRejection) {
    if (isCoordinatorMismatched(opts.coordinatorUrl, opts.sidecarReportedUrl) && opts.sidecarReportedUrl) {
      return {
        message: `The coordinator at ${opts.coordinatorUrl} did not recognize your sign-in. This machine's mesh is on ${opts.sidecarReportedUrl}. Switch and try again?`,
        switchToUrl: opts.sidecarReportedUrl,
      };
    }
    return {
      message: 'The coordinator rejected your sign-in. Try signing out and back in.',
    };
  }

  if (err instanceof CoordinatorError) {
    return {
      message:
        err.httpStatus === 0 || err.httpStatus >= 500
          ? "Couldn't reach the coordinator. Check the connection URL in Settings → Advanced."
          : err.message,
    };
  }
  if (err instanceof MeshApiError) {
    return { message: `${err.code}: ${err.message}` };
  }
  return { message: err instanceof Error ? err.message : 'Something went wrong.' };
}
