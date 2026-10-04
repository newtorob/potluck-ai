#!/usr/bin/env node
// Opt-in real OS-service smoke. Each run uses isolated state and a unique service label.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { execFile, spawn } = require('node:child_process');
const { promisify } = require('node:util');
const run = promisify(execFile);
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'potluck-service-smoke-'));
  const reservation = net.createServer();
  await new Promise(r => reservation.listen(0, '127.0.0.1', r));
  const port = reservation.address().port;
  await new Promise(r => reservation.close(r));
  const entry = process.env.POTLUCK_CLI_ENTRY || path.resolve(__dirname, '../dist/index.js');
  const env = { ...process.env, POTLUCK_DATA_DIR: directory, POTLUCK_SIDECAR_URL: `http://127.0.0.1:${port}`,
    POTLUCK_BIND_HOST: '127.0.0.1', POTLUCK_CLOUD_API_URL: 'http://127.0.0.1:1',
    POTLUCK_LOCAL_API_SOCKET: path.join(directory, 'absent.sock'), NO_COLOR: '1' };
  delete env.POTLUCK_SIDECAR_TOKEN; delete env.POTLUCK_GATEWAY_KEY; delete env.POTLUCK_HOMEBREW;
  const command = async (args, expected = 0) => {
    let result;
    try { result = { ...await run(process.execPath, [entry, ...args, '--json'], { env, timeout: 70000 }), code: 0 }; }
    catch (error) { result = error; }
    assert.equal(result.code, expected, `Command ${args.join(' ')} failed: ${result.stdout || result.stderr}`);
    return JSON.parse(result.stdout);
  };
  const readRecord = async () => { try { return JSON.parse(await fs.readFile(path.join(directory, 'run/runtime.json'), 'utf8')); } catch (e) { if (e.code === 'ENOENT') return null; throw e; } };
  const until = async predicate => {
    for (let i = 0; i < 240; i++) { const result = await predicate(); if (result) return result; await sleep(250); }
    throw new Error('Timed out; inspect private logs in ' + directory);
  };
  const ready = async (owner, oldId) => until(async () => {
    const r = await readRecord();
    if (!r || r.owner !== owner || r.instanceId === oldId) return false;
    try {
      const response = await fetch(r.url + '/control', { headers: { Authorization: 'Bearer ' + r.token }, signal: AbortSignal.timeout(500) });
      return response.ok && (await response.json()).instanceId === r.instanceId ? r : false;
    } catch { return false; }
  });
  const checks = [];
  const check = description => { checks.push(description); console.error('PASS: ' + description); };
  let foreground;
  try {
    const initial = await command(['up', '--timeout', '30000']);
    await command(['models', 'autoload', 'off']);
    const first = await readRecord();
    assert.equal(initial.owner, 'cli');
    // A foreground supervisor may wait for another launcher but may never stop it.
    foreground = spawn(process.execPath, [entry, 'serve'], { env, stdio: 'ignore' });
    await sleep(1500);
    assert.equal((await readRecord()).pid, first.pid);
    const exited = new Promise(r => foreground.once('exit', r));
    foreground.kill('SIGTERM'); await exited; foreground = null;
    assert.equal((await command(['up'])).pid, first.pid);
    check('standby supervisor shutdown leaves the existing CLI runtime alive');

    await command(['service', 'install']);
    await sleep(1500);
    assert.equal((await readRecord()).pid, first.pid);
    await command(['down', '--timeout', '30000']);
    const service = await ready('standalone');
    assert.notEqual(service.instanceId, first.instanceId);
    assert.equal((await command(['up'])).pid, service.pid);
    assert.equal((await command(['service', 'status'])).state, 'running');
    const { discoverRuntime } = require('../../desktop/electron/runtime-control.js');
    assert.equal((await discoverRuntime({ dataDir: directory, port })).pid, service.pid);
    check('OS service waits for CLI ownership, then starts one authenticated runtime shared with app discovery');
    assert.match((await command(['down'], 1)).error.message, /owned by standalone/);
    await command(['service', 'restart']);
    const restarted = await ready('standalone', service.instanceId);
    assert.notEqual(restarted.token, service.token);
    check('service restart changes the instance and credential; CLI down refuses another launcher');
    await command(['service', 'stop']);
    await until(async () => !(await readRecord()));
    await command(['service', 'start']);
    await ready('standalone', restarted.instanceId);
    check('stop drains the child and removes discovery; start works again');
    await command(['service', 'uninstall']);
    await until(async () => !(await readRecord()));
    assert.equal((await command(['service', 'status'])).state, 'not-installed');
    const settings = JSON.parse(await fs.readFile(path.join(directory, 'config.json'), 'utf8'));
    assert.equal(settings.ui['potluck.autoload_on_startup'], 'false');
    check('uninstall removes the service but preserves runtime settings');
    const report = { passed: true, platform: process.platform, runtime: process.env.POTLUCK_RUNTIME_BIN || (process.env.POTLUCK_CLI_ENTRY ? 'installed bundle' : 'source'), checks, directory };
    await fs.writeFile(path.join(directory, 'service-report.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
    console.log(JSON.stringify(report, null, 2));
  } finally {
    foreground?.kill('SIGTERM');
    await command(['service', 'uninstall']).catch(() => {});
    await command(['down', '--timeout', '30000']).catch(() => {});
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
