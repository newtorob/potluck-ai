#!/usr/bin/env node
'use strict';

// Real CLI/runtime smoke on a POSIX host. Uses private temporary state and a
// loopback account fixture; never pairs the host or touches its existing data.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const net = require('node:net');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const run = promisify(execFile);
const ROOT = path.resolve(__dirname, '../../..');
const { discoverRuntime } = require('../../desktop/electron/runtime-control.js');
const { readMemberSession } = require('../../desktop/electron/member-session.js');

async function main() {
  assert.notEqual(process.platform, 'win32');
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'potluck-posix-smoke-'));
  const listener = net.createServer();
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  let approved = false;
  let revoked = false;
  const memberToken = 'fixture-member-bearer-not-a-real-account';
  const deviceToken = 'fixture-device-bearer-not-a-real-account';
  const api = http.createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json');
    const send = (status, body) => { res.writeHead(status); res.end(JSON.stringify(body)); };
    if (req.url === '/auth/device/start') return send(200, {
      device_code: deviceToken, user_code: 'TEST-1234',
      verification_uri: `http://127.0.0.1:${api.address().port}/auth/device`, expires_in: 900, interval: 5,
    });
    if (req.url === '/auth/device/token') return approved
      ? send(200, { token: memberToken, memberId: 'fixture-member', email: 'validation@example.com', expiresAt: new Date(Date.now() + 3600000).toISOString() })
      : send(400, { error: 'authorization_pending' });
    if (req.headers.authorization === `Bearer ${memberToken}` && !revoked) {
      if (req.url === '/me') return send(200, { id: 'fixture-member', email: 'validation@example.com' });
      if (req.url === '/auth/logout') { revoked = true; return send(200, {}); }
    }
    send(401, { detail: 'not signed in' });
  });
  await new Promise(resolve => api.listen(0, '127.0.0.1', resolve));
  const cloud = `http://127.0.0.1:${api.address().port}`;
  const env = { ...process.env, POTLUCK_DATA_DIR: directory, POTLUCK_CLOUD_API_URL: cloud,
    POTLUCK_SIDECAR_URL: `http://127.0.0.1:${port}`, POTLUCK_GATEWAY_URL: `http://127.0.0.1:${port}/v1`,
    POTLUCK_BIND_HOST: '127.0.0.1', POTLUCK_LOCAL_API_SOCKET: path.join(directory, 'absent.sock'), NO_COLOR: '1' };
  delete env.POTLUCK_GATEWAY_KEY;
  delete env.POTLUCK_SIDECAR_TOKEN;
  const recordPath = path.join(directory, 'run/runtime.json');
  const read = async file => JSON.parse(await fs.readFile(path.join(directory, file), 'utf8'));
  const cli = async (args, expected = 0) => {
    let result;
    try { result = { ...await run(process.execPath, [path.join(ROOT, 'apps/cli/dist/index.js'), ...args, '--json'], { env, timeout: 45000, maxBuffer: 2 ** 20 }), code: 0 }; }
    catch (error) { result = error; }
    // Fail without echoing possibly sensitive command output.
    assert.equal(result.code, expected, `CLI ${args.join(' ')} exited ${result.code}; inspect private runtime logs`);
    assert(!result.stdout.includes(memberToken) && !result.stdout.includes(deviceToken), 'CLI exposed a fixture bearer');
    return JSON.parse(result.stdout);
  };
  const checks = [];
  let passed = false;
  try {
    const setup = await cli(['setup', '--no-input', '--enable-gateway', '--timeout', '30000']);
    assert.equal(setup.state, 'configured');
    assert.equal(setup.scope, 'local');
    const first = await read('run/runtime.json');
    const config = await read('config.json');
    const repeats = await Promise.all([cli(['up']), cli(['setup', '--no-input', '--enable-gateway'])]);
    assert.equal(repeats[0].pid, first.pid);
    assert.equal(repeats[1].runtime.pid, first.pid);
    assert.deepEqual(await read('config.json'), config);
    assert.equal((await discoverRuntime({ dataDir: directory, port })).pid, first.pid);
    checks.push('setup defaults to local scope; repeated startup and app discovery share one authenticated engine');

    for (const [file, mode] of [['run', 0o700], ['run/runtime.json', 0o600], ['logs/runtime.log', 0o600]]) {
      assert.equal((await fs.stat(path.join(directory, file))).mode & 0o777, mode);
    }
    const unauthenticated = await fetch(first.url + '/account/status');
    assert.equal(unauthenticated.status, 401);
    await fs.chmod(recordPath, 0o644);
    try { assert.match((await cli(['up'], 1)).error.message, /private/); }
    finally { await fs.chmod(recordPath, 0o600); }
    assert.equal((await cli(['up'])).pid, first.pid);
    checks.push('private control/log permissions enforced; unauthorized account access and unsafe discovery rejected');

    const pending = await cli(['login', '--no-wait']);
    assert.equal(pending.state, 'pending');
    await cli(['down']);
    await assert.rejects(fs.stat(recordPath), { code: 'ENOENT' });
    await cli(['up', '--timeout', '30000']);
    const second = await read('run/runtime.json');
    assert.notEqual(second.token, first.token);
    assert.equal((await fetch(second.url + '/control', { headers: { Authorization: `Bearer ${first.token}` } })).status, 401);
    assert.equal((await cli(['login', '--no-wait'])).userCode, pending.userCode);
    approved = true;
    assert.equal((await cli(['login', '--wait'])).state, 'signed_in');
    assert.equal(readMemberSession(cloud, directory).token, memberToken);
    for (const [file, mode] of [['auth', 0o700], ['auth/member-session.json', 0o600], ['auth/pending-device.json', 0o600]]) {
      assert.equal((await fs.stat(path.join(directory, file))).mode & 0o777, mode);
    }
    checks.push('pending device login survives restart; old control bearer rejected; app session reader sees CLI login');

    await cli(['down']);
    await cli(['up', '--timeout', '30000']);
    assert.equal((await cli(['whoami'])).signedIn, true);
    assert.deepEqual(await read('config.json'), config);
    assert.equal((await cli(['logout'])).revoked, true);
    assert.equal(revoked, true);
    assert.equal((await cli(['whoami'], 1)).signedIn, false);
    assert.equal(readMemberSession(cloud, directory).token, null);
    checks.push('account and gateway settings survive restart; server logout and app reader agree on signed-out state');
    await cli(['down']);
    await assert.rejects(fs.stat(recordPath), { code: 'ENOENT' });
    checks.push('clean shutdown removes the runtime discovery record');
    passed = true;
  } finally {
    await fs.chmod(recordPath, 0o600).catch(() => {});
    await cli(['down']).catch(() => {});
    await new Promise(resolve => api.close(resolve));
    const report = { passed, platform: process.platform, checks, directory,
      runtime: env.POTLUCK_RUNTIME_BIN || 'source checkout',
      limitation: 'Loopback account fixture and Electron reader modules only; no packaged Linux Electron, real cloud device login, GPU or mesh inference coverage.' };
    await fs.writeFile(path.join(directory, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
    console.log(JSON.stringify(report, null, 2));
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
