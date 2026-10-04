#!/usr/bin/env node
'use strict';

// Run against a disposable packaged app with the current main/preload and frozen
// runtime. Requires port 8321 to be free. Never changes the installed application
// or the real ~/.potluck directory. The caller restores any app it closed.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const net = require('node:net');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const run = promisify(execFile);
const { _electron } = require('../../frontend/node_modules/@playwright/test');
const ROOT = path.resolve(__dirname, '../../../..');
const CLI = path.join(ROOT, 'apps/cli/dist/index.js');

async function waitFor(check, description, timeout = 20000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error(`Timed out: ${description}`);
}

async function main() {
  const executable = process.argv[2];
  const runtime = process.argv[3];
  assert(executable && runtime, 'Usage: validate-shared-runtime.cjs <packaged-app-executable> <frozen-runtime-executable>');
  const socket = net.createServer();
  await new Promise((resolve, reject) => {
    socket.once('error', reject); socket.listen(8321, '127.0.0.1', resolve);
  }).catch(() => { throw new Error('Port 8321 is occupied. Close Potluck before running this isolated smoke.'); });
  await new Promise(resolve => socket.close(resolve));
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'potluck-desktop-smoke-'));
  const dataDir = path.join(directory, 'engine');
  const userData = path.join(directory, 'app');
  await fs.mkdir(dataDir, { mode: 0o700 });
  await fs.mkdir(userData, { mode: 0o700 });
  const server = http.createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/client/version') return res.end(JSON.stringify({ latest: '0.1.4', minimum: '0.0.0' }));
    if (req.url === '/me' && req.headers.authorization === 'Bearer fixture-member-session') {
      return res.end(JSON.stringify({ id: '11111111-1111-4111-8111-111111111111', email: 'validation@example.com',
        displayName: 'Validation', avatarUrl: null, membershipPlan: null, membershipStatus: null,
        isFoundingMember: false, isAdmin: false, joinedAt: '2026-01-01T00:00:00Z' }));
    }
    res.writeHead(401); res.end('{"detail":"not signed in"}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const env = { ...process.env, POTLUCK_DATA_DIR: dataDir, POTLUCK_RUNTIME_BIN: runtime,
    POTLUCK_CLOUD_API_URL: `http://127.0.0.1:${server.address().port}`, POTLUCK_BIND_HOST: '127.0.0.1',
    POTLUCK_SIDECAR_URL: 'http://127.0.0.1:8321', POTLUCK_GATEWAY_URL: 'http://127.0.0.1:8321/v1',
    POTLUCK_LOCAL_API_SOCKET: path.join(directory, 'absent.sock'), POTLUCK_DISABLE_DEVTOOLS: '1', NO_COLOR: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.POTLUCK_GATEWAY_KEY;
  const recordPath = path.join(dataDir, 'run/runtime.json');
  const record = async () => JSON.parse(await fs.readFile(recordPath, 'utf8'));
  const cli = async (args, expected = 0) => {
    let stdout;
    try { ({ stdout } = await run(process.execPath, [CLI, ...args, '--json'], { env, timeout: 45000, maxBuffer: 1024 * 1024 })); assert.equal(expected, 0); }
    catch (error) { assert.equal(error.code, expected, `CLI ${args[0]} failed: ${error.stderr || error.stdout || error.message}`); stdout = error.stdout; }
    return JSON.parse(stdout);
  };
  let app;
  const launch = async () => {
    app = await _electron.launch({ executablePath: executable, args: [`--user-data-dir=${userData}`], env, timeout: 30000 });
    assert.equal(await app.evaluate(({ app }) => app.isPackaged), true);
    assert.equal(path.resolve(await app.evaluate(({ app }) => app.getPath('userData'))), await fs.realpath(userData));
    const page = await app.firstWindow();
    await page.waitForLoadState('domcontentloaded');
    await page.waitForFunction(() => Boolean(window.potluck?.cloud));
    return page;
  };
  const close = async () => {
    if (!app) return;
    const instance = app;
    app = undefined;
    let timer;
    try {
      await Promise.race([instance.close(), new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('Packaged app did not quit within 10 seconds')), 10000);
      })]);
    } catch (error) {
      instance.process().kill('SIGTERM');
      throw error;
    } finally { clearTimeout(timer); }
  };
  const checks = [];
  let passed = false;
  try {
    const initial = await cli(['up', '--timeout', '30000']);
    assert.equal(initial.owner, 'cli');
    await fs.writeFile(path.join(userData, 'cloud-session'), 'fixture-member-session', { mode: 0o600 });
    let page = await launch();
    await waitFor(async () => (await cli(['whoami'], 0)).signedIn === true, 'legacy account import');
    assert.equal((await record()).pid, initial.pid);
    assert.equal(await page.evaluate(() => window.potluck.cloud.hasToken()), true);
    assert.equal(await page.evaluate(async () => (await fetch('http://127.0.0.1:8321/settings')).status), 200);
    checks.push('packaged app attaches to CLI engine; renderer authenticates; legacy member session imports');
    await close();
    assert.equal((await cli(['up'])).pid, initial.pid);
    checks.push('quitting attached app leaves CLI engine running');

    page = await launch();
    await cli(['down']);
    const restarted = await cli(['up', '--timeout', '30000']);
    assert.notEqual(restarted.pid, initial.pid);
    await waitFor(async () => page.evaluate(async () => (await fetch('http://127.0.0.1:8321/settings')).status === 200), 'renderer token refresh after CLI restart');
    assert.equal((await cli(['whoami'])).signedIn, true);
    checks.push('CLI engine restart restores account and refreshes renderer control credential');
    await cli(['logout', '--local']);
    assert.equal(await page.evaluate(() => window.potluck.cloud.hasToken()), false);
    assert.equal((await page.evaluate(() => window.potluck.cloud.request('GET', '/me'))).status, 401);
    await close();
    await fs.writeFile(path.join(userData, 'cloud-session'), 'fixture-member-session', { mode: 0o600 });
    page = await launch();
    assert.equal((await cli(['whoami'], 1)).signedIn, false);
    assert.equal(await page.evaluate(() => window.potluck.cloud.hasToken()), false);
    checks.push('CLI logout clears app account; stale legacy session cannot restore it');
    await close();
    await cli(['down']);

    page = await launch();
    const owned = await record();
    assert.equal(owned.owner, 'desktop');
    assert.equal((await cli(['up'])).pid, owned.pid);
    const stopped = await cli(['down'], 1);
    assert.match(stopped.error.message, /owned by desktop/);
    await close();
    await waitFor(async () => { try { await fs.stat(recordPath); return false; } catch (error) { return error.code === 'ENOENT'; } }, 'app-owned engine shutdown');
    checks.push('app starts one owned engine; CLI attaches and refuses to stop it; app quit cleans it up');
    passed = true;
    console.log(JSON.stringify({ passed, checks, directory }, null, 2));
  } finally {
    await close().catch(() => {});
    // Our data directory only; no signal is sent to the user's installed runtime.
    try { await cli(['down']); } catch (_) {}
    await new Promise(resolve => server.close(resolve));
    await fs.writeFile(path.join(directory, 'report.json'), JSON.stringify({ passed, checks }, null, 2));
    console.error(`Validation artifacts: ${directory}`);
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
