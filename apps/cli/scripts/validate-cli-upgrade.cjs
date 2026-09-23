#!/usr/bin/env node
'use strict';
// Real installed-archive upgrade with existing settings, weights and project intent.
// Both extracted bundles and the existing GGUF are supplied explicitly; no downloads.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { createReadStream } = require('node:fs');
const { createHash } = require('node:crypto');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const run = promisify(execFile);

(async () => {
  const oldBundle = process.env.POTLUCK_VALIDATION_OLD_BUNDLE;
  const newBundle = process.env.POTLUCK_VALIDATION_NEW_BUNDLE;
  const weights = process.env.POTLUCK_VALIDATION_MODEL_FILE;
  for (const value of [oldBundle, newBundle, weights]) assert(value && path.isAbsolute(value), 'Supply absolute OLD_BUNDLE, NEW_BUNDLE and MODEL_FILE validation paths.');
  const before = await fs.stat(weights);
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(weights)) hash.update(chunk);
  const sha256 = hash.digest('hex');
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'potluck-cli-upgrade-'));
  const prefix = path.join(directory, 'installed prefix');
  const data = path.join(directory, 'state');
  const project = path.join(directory, 'project');
  const slug = 'upgrade-fixture';
  await fs.mkdir(path.join(data, 'models', slug), { recursive: true });
  await fs.mkdir(path.join(project, '.git'), { recursive: true });
  await fs.symlink(weights, path.join(data, 'models', slug, 'model.gguf'));
  await fs.writeFile(path.join(data, 'config.json'), JSON.stringify({
    runtime: { context_size: 512, gpu_layers: 0, threads: 2, max_tokens: 64 },
    network: { contributor_mode: false },
  }), { mode: 0o600 });
  const listener = net.createServer();
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const env = { ...process.env, POTLUCK_DATA_DIR: data, POTLUCK_BIND_HOST: '127.0.0.1',
    POTLUCK_SIDECAR_URL: `http://127.0.0.1:${port}`, POTLUCK_GATEWAY_URL: `http://127.0.0.1:${port}/v1`,
    POTLUCK_CLOUD_API_URL: 'http://127.0.0.1:9', POTLUCK_CLOUD_API_BASE: 'http://127.0.0.1:9',
    POTLUCK_LOCAL_API_SOCKET: path.join(data, 'absent.sock'), HF_HUB_OFFLINE: '1', NO_COLOR: '1' };
  for (const key of ['POTLUCK_RUNTIME_BIN', 'POTLUCK_GATEWAY_KEY', 'POTLUCK_SIDECAR_TOKEN', 'POTLUCK_MEMORY_DB_PATH', 'POTLUCK_HOMEBREW']) delete env[key];
  const executable = path.join(prefix, 'bin/potluck');
  const cli = async args => {
    const pending = run(executable, [...args, '--json', '--timeout', '120000'], { env, cwd: project, timeout: 150000 });
    pending.child.stdin.end();
    return JSON.parse((await pending).stdout);
  };
  const install = bundle => run(process.execPath, [path.join(bundle, 'install.mjs'), '--prefix', prefix], { env, cwd: directory, timeout: 120000 });
  const read = async file => JSON.parse(await fs.readFile(path.join(data, file), 'utf8'));
  const checks = [];
  let passed = false;
  try {
    await install(oldBundle);
    const oldTarget = await fs.readlink(path.join(prefix, 'lib/potluck/current'));
    await cli(['setup', '--no-input', '--enable-gateway']);
    await cli(['models', 'load', slug]);
    const first = await read('run/runtime.json');
    await cli(['down']);
    // Existing project intent may arrive from source control, independent of CLI install.
    const lockText = JSON.stringify({ schemaVersion: 1, model: { id: slug, format: 'gguf', sha256, sizeBytes: before.size } }, null, 2) + '\n';
    await fs.writeFile(path.join(project, 'potluck.lock'), lockText);
    const settingsText = await fs.readFile(path.join(data, 'config.json'), 'utf8');
    const preferencesText = JSON.stringify({ model: 'different-global-default', scope: 'local' });
    await fs.writeFile(path.join(data, 'cli.json'), preferencesText, { mode: 0o600 });
    await install(newBundle);
    assert.notEqual(await fs.readlink(path.join(prefix, 'lib/potluck/current')), oldTarget);
    await fs.access(path.join(oldTarget, 'manifest.json'));
    assert.equal(await fs.readFile(path.join(data, 'config.json'), 'utf8'), settingsText);
    assert.equal(await fs.readFile(path.join(data, 'cli.json'), 'utf8'), preferencesText);
    assert.equal(await fs.readFile(path.join(project, 'potluck.lock'), 'utf8'), lockText);
    checks.push('real archive upgrade switches the stable command, retains rollback and preserves settings, model files and project lock');
    await cli(['up']);
    const record = await read('run/runtime.json');
    assert.notEqual(record.instanceId, first.instanceId);
    assert.notEqual(record.token, first.token);
    const deadline = Date.now() + 120000;
    while (true) {
      const health = await (await fetch(record.url + '/health', { headers: { Authorization: `Bearer ${record.token}` }, signal: AbortSignal.timeout(5000) })).json();
      if (health.model_restore?.status === 'restored') break;
      assert(Date.now() < deadline && health.model_restore?.status !== 'failed', 'Model restore failed or timed out');
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    assert.equal((await cli(['models', 'sync'])).status, 'verified');
    const answer = await cli(['run', 'Reply with the digit 4. /no_think', '--scope', 'local', '--max-tokens', '64']);
    assert.equal(answer.model, slug);
    assert.equal(answer.sha256, sha256);
    assert(answer.text.trim());
    assert.equal((await read('config.json')).gateway.api_key, JSON.parse(settingsText).gateway.api_key);
    assert.equal((await read('config.json')).network.contributor_mode, false);
    assert.equal(await fs.readFile(path.join(project, 'potluck.lock'), 'utf8'), lockText);
    checks.push('upgraded packaged runtime restores the same model and serves exact pinned bytes despite a conflicting global default');
    const invalid = JSON.parse(lockText); invalid.model.sha256 = '0'.repeat(64);
    await fs.writeFile(path.join(project, 'potluck.lock'), JSON.stringify(invalid));
    try {
      await assert.rejects(cli(['run', 'This must fail before inference', '--scope', 'local']), error => error.code === 1);
    } finally { await fs.writeFile(path.join(project, 'potluck.lock'), lockText); }
    checks.push('wrong project checksum is rejected after upgrade without model substitution');
    const after = await fs.stat(weights);
    assert.equal(after.size, before.size); assert.equal(after.mtimeMs, before.mtimeMs);
    passed = true;
  } finally {
    let stopped = false;
    try { await cli(['down']); stopped = true; } catch { /* retain isolated logs */ }
    const report = { passed: passed && stopped, stopped, directory, platform: process.platform, architecture: process.arch,
      oldBundle, newBundle, sha256, checks, date: new Date().toISOString() };
    await fs.writeFile(path.join(directory, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
    console.log(JSON.stringify(report, null, 2));
    if (!report.passed) process.exitCode = 1;
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
