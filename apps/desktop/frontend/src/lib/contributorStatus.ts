/**
 * What the Contribute compute toggle is doing, as one line of copy.
 *
 * Mirrors GET /contributor/status on the local sidecar. Pure so the copy is
 * testable without rendering; the Network view calls `contributorStatusLine`.
 */

// 'disabled': this build does not join the open pool (see lib/features.ts).
export type ContributorState = 'stopped' | 'connecting' | 'connected' | 'rejected' | 'error' | 'disabled';

export interface ContributorStatus {
  enabled: boolean;
  running: boolean;
  state: ContributorState;
  peer_id: string | null;
  connected_since: string | null;
  last_heartbeat_at: string | null;
  heartbeats_sent: number;
  reconnects: number;
  jobs_served: number;
  jobs_in_flight: number;
  last_error: string | null;
  last_error_detail: string | null;
  coordinator_host: string | null;
  binary_hash_source: string | null;
  max_concurrent_jobs: number | null;
  has_member_session: boolean;
  /** Who this machine serves beyond its owner while connected. */
  audiences?: string[] | null;
}

/** Who the Contribute toggle lets use this machine. */
export type ContributorAudience = 'mine' | 'circles' | 'everyone';

/** The audience in effect when the member has not chosen one: what the
 *  build did before the choice existed. */
export function effectiveAudience(
  chosen: ContributorAudience | null | undefined,
  openPoolBuild: boolean,
): ContributorAudience {
  return chosen ?? (openPoolBuild ? 'everyone' : 'mine');
}

export type ContributorTone = 'ok' | 'pending' | 'warn';

export interface ContributorStatusLine {
  text: string;
  tone: ContributorTone;
}

function jobsPhrase(n: number): string {
  if (n === 0) return 'no jobs served yet';
  if (n === 1) return '1 job served';
  return `${n} jobs served`;
}

/** One sentence for the line under the toggle. Returns null when the toggle
 *  is off, so the card shows nothing extra. */
export function contributorStatusLine(status: ContributorStatus | null): ContributorStatusLine | null {
  if (status === null) return null;
  if (!status.enabled || status.state === 'disabled') return null;

  switch (status.state) {
    case 'connected': {
      const busy = status.jobs_in_flight > 0 ? ', serving now' : '';
      const circlesOnly = Array.isArray(status.audiences) && !status.audiences.includes('pool');
      const lead = circlesOnly ? 'Sharing with your trusted circles' : 'Connected to the network';
      return { text: `${lead} · ${jobsPhrase(status.jobs_served)}${busy}`, tone: 'ok' };
    }
    case 'connecting':
      return status.last_error === 'unreachable'
        ? { text: 'Waiting for the coordinator. Retrying in the background.', tone: 'pending' }
        : { text: 'Connecting to the coordinator', tone: 'pending' };
    case 'rejected':
      switch (status.last_error) {
        case 'device_key_not_registered':
          return {
            text: 'The coordinator does not know this machine yet. Sign in, then turn the toggle off and on to register it.',
            tone: 'warn',
          };
        case 'unauthorized':
          return { text: 'Sign in to contribute. The coordinator did not accept this machine.', tone: 'warn' };
        case 'key_bound_to_other_member':
          return { text: 'This machine is registered to a different member.', tone: 'warn' };
        case 'coordinator_outdated':
          return {
            text: 'The network cannot yet keep this machine to your circles only, so nothing is being shared. It will start once the network is updated.',
            tone: 'warn',
          };
        case 'release_not_registered':
          return { text: 'This build is not on the coordinator’s release list yet.', tone: 'warn' };
        default:
          return {
            text: status.last_error_detail
              ? `The coordinator declined this machine: ${status.last_error_detail}`
              : 'The coordinator declined this machine.',
            tone: 'warn',
          };
      }
    case 'error':
      if (status.last_error === 'coordinator_not_configured') {
        return { text: 'No coordinator address is configured. Set one in Settings.', tone: 'warn' };
      }
      if (status.last_error === 'coordinator_insecure') {
        return { text: 'The coordinator address must use https unless it is on your own network.', tone: 'warn' };
      }
      return { text: 'The contributor stopped unexpectedly. Check the local engine log.', tone: 'warn' };
    case 'stopped':
    default:
      return { text: 'Starting', tone: 'pending' };
  }
}
