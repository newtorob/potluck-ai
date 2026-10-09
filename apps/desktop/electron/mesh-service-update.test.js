'use strict';

// Run with: node --test apps/desktop/electron/
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  findServicePids,
  buildAdminTermScript,
  describeAdminRestartError,
  decideMeshServiceUpdate,
  MISMATCH_GRACE_MS,
  API_RESTART_WINDOW_MS,
  AUTO_RESTART_LIMIT,
  SELF_RESTART_EXIT_CODE,
  FEATURE_RESTART_V1,
} = require('./mesh-service-update');

const BUNDLED = 'a'.repeat(64);
const OLDER = 'b'.repeat(64);
const NOW = 1_800_000_000_000;

// A /status body from each generation of sidecar.
const noHash = { mesh_status: 'running', version: '0.1.0' }; // before exe_sha256
const hashOnly = (hash) => ({ mesh_status: 'running', exe_sha256: hash, features: [] }); // watcher, no command
const withCommand = (hash) => ({ mesh_status: 'running', exe_sha256: hash, features: ['circle_link_v1', FEATURE_RESTART_V1] });

function decide(overrides = {}) {
  return decideMeshServiceUpdate({
    platform: 'linux',
    bundledSha256: BUNDLED,
    status: withCommand(BUNDLED),
    mismatchSeen: null,
    now: NOW,
    apiRestart: null,
    autoRestarts: 0,
    isAppImage: false,
    ...overrides,
  });
}

// Grace period already over for a sidecar reporting OLDER.
const graceOver = { sha256: OLDER, at: NOW - MISMATCH_GRACE_MS };

function assertQuiet(d, verdict) {
  assert.equal(d.verdict, verdict);
  assert.equal(d.mechanism, 'none');
  assert.equal(d.automatic, false);
  assert.equal(d.surface, false);
}

test('the flags never say "do it now" and "tell the user" together', () => {
  for (const platform of ['darwin', 'linux', 'win32']) {
    for (const status of [noHash, hashOnly(OLDER), withCommand(OLDER), withCommand(BUNDLED), hashOnly('')]) {
      for (const apiRestart of [null, { sha256: OLDER, at: NOW - 1000 }, { sha256: OLDER, at: NOW - 10 * API_RESTART_WINDOW_MS }]) {
        for (const autoRestarts of [0, AUTO_RESTART_LIMIT]) {
          const d = decide({ platform, status, mismatchSeen: graceOver, apiRestart, autoRestarts });
          assert.ok(!(d.automatic && d.surface), `${platform} ${JSON.stringify(status)}`);
        }
      }
    }
  }
});

for (const platform of ['darwin', 'linux', 'win32']) {
  test(`${platform}: the service runs the bundled binary`, () => {
    const d = decide({ platform });
    assertQuiet(d, 'current');
    assert.equal(d.mismatchSeen, null);
  });

  test(`${platform}: a mismatch seen earlier is forgotten once the hashes match`, () => {
    const d = decide({ platform, mismatchSeen: { sha256: OLDER, at: NOW - 60_000 } });
    assertQuiet(d, 'current');
    assert.equal(d.mismatchSeen, null);
  });

  test(`${platform}: hashes compare without regard to case or padding`, () => {
    assertQuiet(decide({ platform, status: withCommand(`  ${BUNDLED.toUpperCase()}\n`) }), 'current');
  });

  test(`${platform}: a sidecar with no hash is outdated at once and nothing is automatic`, () => {
    const d = decide({ platform, status: noHash });
    assert.equal(d.verdict, 'outdated');
    assert.equal(d.surface, true);
    assert.equal(d.automatic, false);
    assert.equal(d.mechanism, platform === 'darwin' ? 'mac_admin_restart' : 'offer_restart');
  });

  test(`${platform}: a null or non-string hash counts as missing`, () => {
    for (const bad of [null, 7, {}, []]) {
      const d = decide({ platform, status: { mesh_status: 'running', exe_sha256: bad } });
      assert.equal(d.verdict, 'outdated', JSON.stringify(bad));
      assert.equal(d.automatic, false);
    }
  });

  test(`${platform}: a different hash is given ${MISMATCH_GRACE_MS / 1000}s to fix itself`, () => {
    const status = withCommand(OLDER);
    const first = decide({ platform, status });
    assertQuiet(first, 'settling');
    assert.deepEqual(first.mismatchSeen, { sha256: OLDER, at: NOW });

    const later = decide({ platform, status, mismatchSeen: first.mismatchSeen, now: NOW + MISMATCH_GRACE_MS - 1 });
    assertQuiet(later, 'settling');
    assert.deepEqual(later.mismatchSeen, first.mismatchSeen, 'the clock keeps its first reading');

    const expired = decide({ platform, status, mismatchSeen: first.mismatchSeen, now: NOW + MISMATCH_GRACE_MS });
    assert.equal(expired.verdict, 'outdated');
  });

  test(`${platform}: a different process with another differing hash starts its own grace period`, () => {
    const d = decide({
      platform,
      status: withCommand('c'.repeat(64)),
      mismatchSeen: { sha256: OLDER, at: NOW - 10 * MISMATCH_GRACE_MS },
    });
    assertQuiet(d, 'settling');
    assert.deepEqual(d.mismatchSeen, { sha256: 'c'.repeat(64), at: NOW });
  });

  test(`${platform}: nothing is decided without an answer from the service`, () => {
    const d = decide({ platform, status: null, mismatchSeen: { sha256: OLDER, at: NOW - 1000 } });
    assertQuiet(d, 'unknown');
    assert.equal(d.mismatchSeen, null, 'a restart in between starts the count over');
  });

  test(`${platform}: no bundled hash means no action, even for an old sidecar`, () => {
    for (const bundledSha256 of [null, '', undefined]) {
      assertQuiet(decide({ platform, bundledSha256, status: noHash }), 'unknown');
    }
  });

  test(`${platform}: a current sidecar that could not hash itself is left alone`, () => {
    assertQuiet(decide({ platform, status: hashOnly('') }), 'unknown');
  });

  test(`${platform}: an answer that is not a /status body is not judged`, () => {
    for (const status of [{ raw: '<html>' }, {}, 'text']) {
      assertQuiet(decide({ platform, status }), 'unknown');
    }
  });
}

// ---- sidecar with the restart command (restart_v1) -------------------------

for (const platform of ['darwin', 'linux']) {
  test(`${platform}: after the grace period a sidecar with the command is restarted through it, silently`, () => {
    const d = decide({ platform, status: withCommand(OLDER), mismatchSeen: graceOver });
    assert.equal(d.verdict, 'outdated');
    assert.equal(d.mechanism, 'api_restart');
    assert.equal(d.automatic, true);
    assert.equal(d.surface, false, 'no banner while the app is about to fix it');
  });

  test(`${platform}: while the restart is awaited nothing more is started and nothing is shown`, () => {
    const apiRestart = { sha256: OLDER, at: NOW - (API_RESTART_WINDOW_MS - 1) };
    const d = decide({ platform, status: withCommand(OLDER), mismatchSeen: graceOver, apiRestart, autoRestarts: 1 });
    assert.equal(d.mechanism, 'api_restart');
    assert.equal(d.automatic, false);
    assert.equal(d.surface, false);
  });

  test(`${platform}: still the old hash after the restart window is surfaced, not restarted again`, () => {
    const apiRestart = { sha256: OLDER, at: NOW - API_RESTART_WINDOW_MS };
    const d = decide({ platform, status: withCommand(OLDER), mismatchSeen: graceOver, apiRestart, autoRestarts: 1 });
    assert.equal(d.verdict, 'outdated');
    assert.equal(d.automatic, false, 'never a loop');
    assert.equal(d.surface, true);
    // macOS: the button runs the same restart again. Linux: the elevated helper.
    assert.equal(d.mechanism, platform === 'darwin' ? 'api_restart' : 'offer_restart');
  });

  test(`${platform}: the allowance of automatic restarts per launch is respected`, () => {
    const d = decide({ platform, status: withCommand(OLDER), mismatchSeen: graceOver, autoRestarts: AUTO_RESTART_LIMIT });
    assert.equal(d.automatic, false);
    assert.equal(d.surface, true);
  });

  test(`${platform}: a restart that was for another hash does not count for this one`, () => {
    const apiRestart = { sha256: 'c'.repeat(64), at: NOW - 10 * API_RESTART_WINDOW_MS };
    const d = decide({ platform, status: withCommand(OLDER), mismatchSeen: graceOver, apiRestart, autoRestarts: 1 });
    assert.equal(d.automatic, true);
  });

  test(`${platform}: a sidecar with the command is not restarted before the grace period is over`, () => {
    const d = decide({ platform, status: withCommand(OLDER), mismatchSeen: { sha256: OLDER, at: NOW - 1000 } });
    assertQuiet(d, 'settling');
  });
}

// ---- sidecar that reports a hash but has no restart command ---------------

test('darwin: a sidecar that only watches its file gets the administrator restart behind the button', () => {
  const d = decide({ platform: 'darwin', status: hashOnly(OLDER), mismatchSeen: graceOver });
  assert.equal(d.verdict, 'outdated');
  assert.equal(d.mechanism, 'mac_admin_restart');
  assert.equal(d.automatic, false);
  assert.equal(d.surface, true);
});

test('linux: a sidecar that only watches its file gets the elevated helper behind the button', () => {
  const d = decide({ platform: 'linux', status: hashOnly(OLDER), mismatchSeen: graceOver });
  assert.equal(d.mechanism, 'offer_restart');
  assert.equal(d.automatic, false);
  assert.equal(d.surface, true);
});

test('win32: the restart command is never used, its service runs a copy only the helper replaces', () => {
  const d = decide({ platform: 'win32', status: withCommand(OLDER), mismatchSeen: graceOver });
  assert.equal(d.mechanism, 'offer_restart');
  assert.equal(d.automatic, false);
  assert.equal(d.surface, true);
});

test('a features list that is missing or not a list never enables the command', () => {
  for (const features of [undefined, null, 'restart_v1', { restart_v1: true }]) {
    const d = decide({
      platform: 'linux',
      status: { mesh_status: 'running', exe_sha256: OLDER, features },
      mismatchSeen: graceOver,
    });
    assert.equal(d.automatic, false, JSON.stringify(features));
    assert.equal(d.mechanism, 'offer_restart');
  }
});

test('linux AppImage: the install helper refuses the mount path, so nothing is offered', () => {
  assertQuiet(decide({ platform: 'linux', status: noHash, isAppImage: true }), 'unknown');
});

test('isAppImage only matters on linux', () => {
  const d = decide({ platform: 'darwin', status: noHash, isAppImage: true });
  assert.equal(d.mechanism, 'mac_admin_restart');
});

test('other platforms are never judged', () => {
  assertQuiet(decide({ platform: 'freebsd', status: noHash }), 'unknown');
});

test('the self-restart exit code and the feature name match the Go sources', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const goDir = path.join(__dirname, '..', 'go', 'potluck-mesh');
  const selfRestart = fs.readFileSync(path.join(goDir, 'cmd', 'potluck-mesh', 'selfrestart.go'), 'utf8');
  const exit = /exitSelfRestart\s*=\s*(\d+)/.exec(selfRestart);
  assert.ok(exit, 'exitSelfRestart not found in selfrestart.go');
  assert.equal(SELF_RESTART_EXIT_CODE, Number(exit[1]));

  const server = fs.readFileSync(path.join(goDir, 'internal', 'localapi', 'server.go'), 'utf8');
  const feature = /FeatureRestartV1\s*=\s*"([^"]+)"/.exec(server);
  assert.ok(feature, 'FeatureRestartV1 not found in server.go');
  assert.equal(FEATURE_RESTART_V1, feature[1]);
});

// ---- macOS: finding and signalling the old process -------------------------

const SERVICE_BIN = '/Applications/Potluck AI.app/Contents/Resources/mesh/potluck-mesh';
const PROMPT = 'Potluck needs to restart its mesh service to finish the update.';

test('findServicePids: one root process running exactly the service binary', () => {
  const ps = [
    '    1     0 /sbin/launchd',
    `  6852     0 ${SERVICE_BIN}`,
    '  7000   501 /Applications/Potluck AI.app/Contents/MacOS/Potluck AI',
    '',
  ].join('\n');
  assert.deepEqual(findServicePids(ps, SERVICE_BIN), [6852]);
});

test('findServicePids: a process owned by anyone but root is not the service', () => {
  assert.deepEqual(findServicePids(`  6852   501 ${SERVICE_BIN}\n  6853   502 ${SERVICE_BIN}`, SERVICE_BIN), []);
});

test('findServicePids: the path must match exactly', () => {
  const near = [
    `${SERVICE_BIN}-other`,
    `${SERVICE_BIN}/extra`,
    '/Applications/Potluck AI.app/Contents/MacOS/mesh-helper',
    '/Applications/Potluck AI 2.app/Contents/Resources/mesh/potluck-mesh',
    '/tmp/potluck-mesh',
    'potluck-mesh',
    SERVICE_BIN.toUpperCase(),
  ];
  const ps = near.map((p, i) => `  ${100 + i}     0 ${p}`).join('\n');
  assert.deepEqual(findServicePids(ps, SERVICE_BIN), []);
});

test('findServicePids: two matches are both reported, so the caller can refuse', () => {
  const ps = `  6852     0 ${SERVICE_BIN}\n  9001     0 ${SERVICE_BIN}`;
  assert.deepEqual(findServicePids(ps, SERVICE_BIN), [6852, 9001]);
});

test('findServicePids: ignores pids that are not real, malformed lines and non-text', () => {
  const ps = [
    `     0     0 ${SERVICE_BIN}`,
    `     1     0 ${SERVICE_BIN}`,
    `    -5     0 ${SERVICE_BIN}`,
    `  abc     0 ${SERVICE_BIN}`,
    `  12.5    0 ${SERVICE_BIN}`,
    `  99999999999999999999 0 ${SERVICE_BIN}`,
    `  4242     0`,
    `  4243     0 ${SERVICE_BIN}`,
  ].join('\n');
  assert.deepEqual(findServicePids(ps, SERVICE_BIN), [4243]);
  assert.deepEqual(findServicePids(undefined, SERVICE_BIN), []);
  assert.deepEqual(findServicePids(null, SERVICE_BIN), []);
});

test('buildAdminTermScript: fixed command, the pid is the only variable part', () => {
  const a = buildAdminTermScript(4242, SERVICE_BIN, PROMPT);
  assert.equal(
    a,
    `do shell script "case $(/bin/ps -p 4242 -o comm=) in '${SERVICE_BIN}') /bin/kill -TERM 4242;; *) exit 3;; esac" ` +
      `with prompt "${PROMPT}" with administrator privileges`,
  );
  assert.match(a, /with administrator privileges$/);
  assert.ok(a.includes(PROMPT));
});

test('buildAdminTermScript: refuses anything but a real numeric pid', () => {
  for (const bad of [0, 1, -3, 1.5, NaN, Infinity, '4242', '4242; rm -rf /', null, undefined, {}, [], 2 ** 60]) {
    assert.throws(() => buildAdminTermScript(bad, SERVICE_BIN, PROMPT), /invalid pid/, String(bad));
  }
});

test('buildAdminTermScript: constants that could break out of the script are refused', () => {
  for (const bad of ['a"b', "a'b", 'a\\b', 'a\nb', 'a$b', 'a`b']) {
    assert.throws(() => buildAdminTermScript(4242, bad, PROMPT), /cannot be embedded/);
    assert.throws(() => buildAdminTermScript(4242, SERVICE_BIN, bad), /cannot be embedded/);
  }
});

test('describeAdminRestartError: a cancelled dialog reads as authentication cancelled', () => {
  assert.equal(describeAdminRestartError('0:93: execution error: User canceled. (-128)'), 'authentication cancelled');
  assert.equal(describeAdminRestartError('execution error: User canceled.'), 'authentication cancelled');
});

test('describeAdminRestartError: exit 3 means the pid no longer runs the service', () => {
  assert.match(describeAdminRestartError('0:150: execution error: The command exited with a non-zero status. (3)\n'), /changed while the dialog was open/);
});

test('describeAdminRestartError: anything else gives the last line, or a plain fallback', () => {
  assert.equal(describeAdminRestartError('first\nsecond failure\n'), 'second failure');
  assert.equal(describeAdminRestartError(''), 'the administrator restart failed');
  assert.equal(describeAdminRestartError(undefined), 'the administrator restart failed');
});
