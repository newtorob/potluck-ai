import { describe, expect, it } from 'vitest';
import { contributorStatusLine, effectiveAudience, type ContributorStatus } from './contributorStatus';

function status(over: Partial<ContributorStatus>): ContributorStatus {
  return {
    enabled: true,
    running: true,
    state: 'connected',
    peer_id: 'p',
    connected_since: null,
    last_heartbeat_at: null,
    heartbeats_sent: 0,
    reconnects: 0,
    jobs_served: 0,
    jobs_in_flight: 0,
    last_error: null,
    last_error_detail: null,
    coordinator_host: 'coord',
    binary_hash_source: 'dev',
    max_concurrent_jobs: 1,
    has_member_session: true,
    ...over,
  };
}

describe('contributorStatusLine', () => {
  it('says nothing when the toggle is off or status is unknown', () => {
    expect(contributorStatusLine(null)).toBeNull();
    expect(contributorStatusLine(status({ enabled: false, state: 'stopped' }))).toBeNull();
  });

  it('counts served jobs when connected', () => {
    expect(contributorStatusLine(status({ jobs_served: 0 }))?.text).toBe(
      'Connected to the network · no jobs served yet',
    );
    expect(contributorStatusLine(status({ jobs_served: 1 }))?.text).toBe(
      'Connected to the network · 1 job served',
    );
    const busy = contributorStatusLine(status({ jobs_served: 3, jobs_in_flight: 1 }));
    expect(busy?.text).toBe('Connected to the network · 3 jobs served, serving now');
    expect(busy?.tone).toBe('ok');
  });

  it('distinguishes connecting from an unreachable coordinator', () => {
    expect(contributorStatusLine(status({ state: 'connecting' }))?.tone).toBe('pending');
    expect(
      contributorStatusLine(status({ state: 'connecting', last_error: 'unreachable' }))?.text,
    ).toMatch(/Waiting for the coordinator/);
  });

  it('explains each reject category in plain words', () => {
    const unregistered = contributorStatusLine(
      status({ state: 'rejected', last_error: 'device_key_not_registered' }),
    );
    expect(unregistered?.tone).toBe('warn');
    expect(unregistered?.text).toMatch(/does not know this machine/);
    expect(
      contributorStatusLine(status({ state: 'rejected', last_error: 'unauthorized' }))?.text,
    ).toMatch(/Sign in/);
    expect(
      contributorStatusLine(status({ state: 'rejected', last_error: 'rejected', last_error_detail: 'nonce_signature: does not verify' }))?.text,
    ).toBe('The coordinator declined this machine: nonce_signature: does not verify');
  });

  it('names the missing coordinator address', () => {
    expect(
      contributorStatusLine(status({ state: 'error', last_error: 'coordinator_not_configured' }))?.text,
    ).toMatch(/No coordinator address/);
  });

  it('explains an insecure coordinator address', () => {
    expect(
      contributorStatusLine(status({ state: 'error', last_error: 'coordinator_insecure' }))?.text,
    ).toMatch(/must use https/);
  });

  it('never uses an em dash', () => {
    const states: Array<Partial<ContributorStatus>> = [
      {},
      { state: 'connecting' },
      { state: 'connecting', last_error: 'unreachable' },
      { state: 'rejected', last_error: 'device_key_not_registered' },
      { state: 'rejected', last_error: 'unauthorized' },
      { state: 'rejected', last_error: 'key_bound_to_other_member' },
      { state: 'rejected', last_error: 'release_not_registered' },
      { state: 'rejected', last_error: 'rejected' },
      { state: 'error', last_error: 'coordinator_not_configured' },
      { state: 'error', last_error: 'loop_crashed' },
      { state: 'stopped' },
      { pause_reason: 'model_not_verified' },
    ];
    for (const s of states) {
      expect(contributorStatusLine(status(s))?.text ?? '').not.toContain('—');
    }
  });

  it('says lending is paused when the loaded model has not passed its answer check', () => {
    const line = contributorStatusLine(status({ pause_reason: 'model_not_verified', jobs_served: 4 }));
    expect(line).toEqual({
      text: 'Lending is paused because this model has not passed its answer check on this machine.',
      tone: 'warn',
    });
  });

  it('keeps the usual connected line for a pause reason the app has no sentence for, or none', () => {
    const reasons: Array<Partial<ContributorStatus>> = [
      {},
      { pause_reason: null },
      { pause_reason: 'low_memory' },
      { pause_reason: 'outside_schedule' },
      { pause_reason: '__proto__' },
      { pause_reason: 'constructor' },
      { pause_reason: 'toString' },
    ];
    for (const reason of reasons) {
      expect(contributorStatusLine(status({ jobs_served: 2, ...reason }))?.text).toBe(
        'Connected to the network · 2 jobs served',
      );
    }
  });

  it('shows the pause only while connected: other states keep their own line', () => {
    expect(contributorStatusLine(status({ state: 'connecting', pause_reason: 'model_not_verified' }))?.text)
      .toBe('Connecting to the coordinator');
    expect(contributorStatusLine(status({ enabled: false, state: 'stopped', pause_reason: 'model_not_verified' }))).toBeNull();
  });

  it('says who is being served when the machine is shared with circles only', () => {
    expect(contributorStatusLine(status({ audiences: ['circle'], jobs_served: 2 }))?.text)
      .toBe('Sharing with your trusted circles · 2 jobs served');
    expect(contributorStatusLine(status({ audiences: ['circle', 'pool'] }))?.text)
      .toContain('Connected to the network');
  });

  it('explains a coordinator that cannot limit sharing, and that nothing is shared', () => {
    const line = contributorStatusLine(status({ state: 'rejected', last_error: 'coordinator_outdated' }));
    expect(line?.tone).toBe('warn');
    expect(line?.text).toContain('nothing is being shared');
  });
});

describe('effectiveAudience', () => {
  it('keeps what the build did when nothing was chosen', () => {
    expect(effectiveAudience(null, false)).toBe('mine');
    expect(effectiveAudience(undefined, true)).toBe('everyone');
    expect(effectiveAudience('circles', true)).toBe('circles');
  });
});
