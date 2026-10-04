import { describe, expect, it } from 'vitest';
import { FEATURES } from './features';
import { contributorStatusLine } from './contributorStatus';

describe('unreleased features', () => {
  it('are all off unless a build turns them on', () => {
    expect(FEATURES).toEqual({ agentWorkroom: false, managedSplit: false, openPoolContributor: false });
  });

  it('show no open-pool status line for a disabled contributor', () => {
    expect(contributorStatusLine({
      enabled: true, state: 'disabled', peer_id: null, coordinator_host: null, last_error: null,
      last_error_detail: null, jobs_served: 0, jobs_in_flight: 0, connected_since: null, heartbeats: 0,
    } as never)).toBeNull();
  });
});
