'use strict';

// Run with: node --test apps/desktop/electron/
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  decideMeshServiceUpdate,
  MISMATCH_GRACE_MS,
  SELF_RESTART_EXIT_CODE,
} = require('./mesh-service-update');

const BUNDLED = 'a'.repeat(64);
const OLDER = 'b'.repeat(64);
const NOW = 1_800_000_000_000;

function decide(overrides = {}) {
  return decideMeshServiceUpdate({
    platform: 'linux',
    bundledSha256: BUNDLED,
    status: { mesh_status: 'running', exe_sha256: BUNDLED },
    mismatchSeen: null,
    now: NOW,
    macRestartAttempted: false,
    isAppImage: false,
    ...overrides,
  });
}

const withHash = (exe_sha256) => ({ mesh_status: 'running', exe_sha256 });
const withoutHash = { mesh_status: 'running', version: '0.1.0' };

for (const platform of ['darwin', 'linux', 'win32']) {
  test(`${platform}: the service runs the bundled binary`, () => {
    const d = decide({ platform });
    assert.equal(d.verdict, 'current');
    assert.equal(d.action, 'none');
    assert.equal(d.mismatchSeen, null);
  });

  test(`${platform}: a mismatch seen earlier is forgotten once the hashes match`, () => {
    const d = decide({ platform, mismatchSeen: { sha256: OLDER, at: NOW - 60_000 } });
    assert.equal(d.verdict, 'current');
    assert.equal(d.mismatchSeen, null);
  });

  test(`${platform}: hashes compare without regard to case or padding`, () => {
    const d = decide({ platform, status: withHash(`  ${BUNDLED.toUpperCase()}\n`) });
    assert.equal(d.verdict, 'current');
  });

  test(`${platform}: no hash in /status means an older sidecar, act at once`, () => {
    const d = decide({ platform, status: withoutHash });
    assert.equal(d.verdict, 'outdated');
    assert.equal(d.action, platform === 'darwin' ? 'restart_mac' : 'offer_restart');
  });

  test(`${platform}: a null or non-string hash counts as missing`, () => {
    for (const bad of [null, 7, {}, []]) {
      const d = decide({ platform, status: withHash(bad) });
      assert.equal(d.verdict, 'outdated', JSON.stringify(bad));
    }
  });

  test(`${platform}: a different hash is given ${MISMATCH_GRACE_MS / 1000}s to fix itself`, () => {
    const first = decide({ platform, status: withHash(OLDER) });
    assert.equal(first.verdict, 'settling');
    assert.equal(first.action, 'none');
    assert.deepEqual(first.mismatchSeen, { sha256: OLDER, at: NOW });

    const later = decide({
      platform,
      status: withHash(OLDER),
      mismatchSeen: first.mismatchSeen,
      now: NOW + MISMATCH_GRACE_MS - 1,
    });
    assert.equal(later.verdict, 'settling');
    assert.equal(later.action, 'none');
    assert.deepEqual(later.mismatchSeen, first.mismatchSeen, 'the clock keeps its first reading');

    const expired = decide({
      platform,
      status: withHash(OLDER),
      mismatchSeen: first.mismatchSeen,
      now: NOW + MISMATCH_GRACE_MS,
    });
    assert.equal(expired.verdict, 'outdated');
    assert.equal(expired.action, platform === 'darwin' ? 'restart_mac' : 'offer_restart');
  });

  test(`${platform}: a different process with another differing hash starts its own grace period`, () => {
    const d = decide({
      platform,
      status: withHash('c'.repeat(64)),
      mismatchSeen: { sha256: OLDER, at: NOW - 10 * MISMATCH_GRACE_MS },
    });
    assert.equal(d.verdict, 'settling');
    assert.deepEqual(d.mismatchSeen, { sha256: 'c'.repeat(64), at: NOW });
  });

  test(`${platform}: nothing is decided without an answer from the service`, () => {
    const d = decide({ platform, status: null, mismatchSeen: { sha256: OLDER, at: NOW - 1000 } });
    assert.equal(d.verdict, 'unknown');
    assert.equal(d.action, 'none');
    assert.equal(d.mismatchSeen, null, 'a restart in between starts the count over');
  });

  test(`${platform}: no bundled hash means no action, even for an old sidecar`, () => {
    for (const bundledSha256 of [null, '', undefined]) {
      const d = decide({ platform, bundledSha256, status: withoutHash });
      assert.equal(d.verdict, 'unknown');
      assert.equal(d.action, 'none');
    }
  });

  test(`${platform}: a current sidecar that could not hash itself is left alone`, () => {
    const d = decide({ platform, status: withHash('') });
    assert.equal(d.verdict, 'unknown');
    assert.equal(d.action, 'none');
  });

  test(`${platform}: an answer that is not a /status body is not judged`, () => {
    for (const status of [{ raw: '<html>' }, {}, 'text']) {
      const d = decide({ platform, status });
      assert.equal(d.verdict, 'unknown');
      assert.equal(d.action, 'none');
    }
  });
}

test('darwin: the automatic restart happens once, then the state is surfaced', () => {
  const before = decide({ platform: 'darwin', status: withoutHash });
  assert.equal(before.action, 'restart_mac');

  const after = decide({ platform: 'darwin', status: withoutHash, macRestartAttempted: true });
  assert.equal(after.verdict, 'outdated');
  assert.equal(after.action, 'offer_restart');
});

test('darwin: after the one attempt a differing hash is surfaced, not restarted again', () => {
  const d = decide({
    platform: 'darwin',
    status: withHash(OLDER),
    macRestartAttempted: true,
    mismatchSeen: { sha256: OLDER, at: NOW - 2 * MISMATCH_GRACE_MS },
  });
  assert.equal(d.action, 'offer_restart');
});

test('linux and win32 never ask for an automatic restart', () => {
  for (const platform of ['linux', 'win32']) {
    for (const status of [withoutHash, withHash(OLDER)]) {
      const d = decide({
        platform,
        status,
        mismatchSeen: { sha256: OLDER, at: NOW - 2 * MISMATCH_GRACE_MS },
      });
      assert.notEqual(d.action, 'restart_mac', platform);
    }
  }
});

test('linux AppImage: the install helper refuses the mount path, so nothing is offered', () => {
  const d = decide({ platform: 'linux', status: withoutHash, isAppImage: true });
  assert.equal(d.verdict, 'unknown');
  assert.equal(d.action, 'none');
});

test('isAppImage only matters on linux', () => {
  const d = decide({ platform: 'win32', status: withoutHash, isAppImage: true });
  assert.equal(d.action, 'offer_restart');
});

test('other platforms are never judged', () => {
  const d = decide({ platform: 'freebsd', status: withoutHash });
  assert.equal(d.verdict, 'unknown');
  assert.equal(d.action, 'none');
});

test('the self-restart exit code matches exitSelfRestart in selfrestart.go', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const goSource = fs.readFileSync(
    path.join(__dirname, '..', 'go', 'potluck-mesh', 'cmd', 'potluck-mesh', 'selfrestart.go'),
    'utf8',
  );
  const match = /exitSelfRestart\s*=\s*(\d+)/.exec(goSource);
  assert.ok(match, 'exitSelfRestart not found in selfrestart.go');
  assert.equal(SELF_RESTART_EXIT_CODE, Number(match[1]));
});
