#!/usr/bin/env node
'use strict';
// Isolated real-runtime validation. Existing GGUF weights are only read via symlinks.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { pathToFileURL } = require('node:url');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const run = promisify(execFile);
const root = path.resolve(__dirname, '../../..');

async function main() {
  const weights = process.env.POTLUCK_VALIDATION_MODEL_FILE;
  assert(weights && path.isAbsolute(weights), 'Set POTLUCK_VALIDATION_MODEL_FILE to an existing GGUF.');
  const before = await fs.stat(weights);
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'potluck-model-setup-'));
  const project = path.join(directory, 'project');
  const nested = path.join(project, 'src');
  await fs.mkdir(nested, { recursive: true });
  await fs.mkdir(path.join(project, '.git'));
  const listener = net.createServer();
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const slug = 'lock-fixture';
  for (const id of [slug, 'other-fixture']) {
    const folder = path.join(directory, 'models', id);
    await fs.mkdir(folder, { recursive: true });
    await fs.symlink(weights, path.join(folder, 'model.gguf'));
  }
  await fs.writeFile(path.join(directory, 'config.json'), JSON.stringify({
    runtime: { context_size: 512, gpu_layers: 0, threads: 2, max_tokens: 16 },
    network: { contributor_mode: false },
  }), { mode: 0o600 });
  await fs.writeFile(path.join(directory, 'cli.json'), JSON.stringify({ model: 'other-fixture', scope: 'local' }));
  const env = { ...process.env, POTLUCK_DATA_DIR: directory,
    POTLUCK_CLOUD_API_URL: 'http://127.0.0.1:9', POTLUCK_CLOUD_API_BASE: 'http://127.0.0.1:9',
    POTLUCK_SIDECAR_URL: `http://127.0.0.1:${port}`, POTLUCK_GATEWAY_URL: `http://127.0.0.1:${port}/v1`,
    POTLUCK_BIND_HOST: '127.0.0.1', HF_HUB_OFFLINE: '1',
    POTLUCK_LOCAL_API_SOCKET: path.join(directory, 'absent.sock'), NO_COLOR: '1' };
  for (const key of ['POTLUCK_GATEWAY_KEY', 'POTLUCK_SIDECAR_TOKEN', 'POTLUCK_MEMORY_DB_PATH']) delete env[key];
  const entry = path.join(root, 'apps/cli/dist/index.js');
  const cli = async args => JSON.parse((await run(process.execPath,
    [entry, ...args, '--json', '--timeout', '30000'], { env, cwd: nested, timeout: 45000 })).stdout);
  const read = async file => JSON.parse(await fs.readFile(path.join(directory, file), 'utf8'));
  const checks = [];
  let passed = false;
  try {
    const setup = await cli(['setup', '--no-input', '--enable-gateway', '--model', slug]);
    assert.equal(setup.state, 'model_ready');
    assert.equal(setup.model.model, slug);
    assert.equal(setup.model.scope, 'local');
    assert.equal((await read('cli.json')).model, slug);
    const again = await cli(['setup', '--no-input', '--model', slug]);
    assert.equal(again.model.status, 'ready');
    checks.push('unattended setup loads and tests installed weights, persists successful selection and repeats safely');
    await cli(['models', 'pin', slug]);
    const saved = await fs.readFile(path.join(project, 'potluck.lock'), 'utf8');
    const lock = JSON.parse(saved);
    assert.match(lock.model.sha256, /^[a-f0-9]{64}$/);
    assert.equal(lock.model.sizeBytes, before.size);
    assert.equal((await cli(['models', 'lock'])).model, slug);
    assert.equal((await cli(['models', 'sync'])).status, 'verified');
    checks.push('CLI pins verified bytes at project root and sync verifies existing weights');
    await assert.rejects(run(process.execPath, [entry, '-m', 'other-fixture', '-p', 'hi'], { env, cwd: nested }), error => {
      assert.equal(error.code, 1); assert.match(error.stderr, /project pins/); return true;
    });
    const pinned = await cli(['setup', '--no-input', '--with-model']);
    assert.equal(pinned.model.pinned, true);
    await cli(['down']); await cli(['up']);
    const deadline = Date.now() + 60000;
    while (true) {
      const record = await read('run/runtime.json');
      const health = await (await fetch(record.url + '/health', { headers: { Authorization: `Bearer ${record.token}` } })).json();
      if (health.model_restore?.status === 'restored') break;
      assert(Date.now() < deadline && health.model_restore?.status !== 'failed', 'Model restore failed or timed out');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.equal(await fs.readFile(path.join(project, 'potluck.lock'), 'utf8'), saved);
    const settings = await read('config.json');
    const cfg = { baseURL: env.POTLUCK_GATEWAY_URL, apiKey: settings.gateway.api_key, enabled: true,
      modelLock: { ...lock, path: path.join(project, 'potluck.lock') } };
    const { streamChat } = await import(pathToFileURL(path.join(root, 'apps/cli/dist/gateway.js')));
    let output = '';
    for await (const token of streamChat(cfg, [{ role: 'user', content: 'Reply with the digit 4. /no_think' }], {
      model: slug, scope: 'local', maxTokens: 64, temperature: 0, signal: AbortSignal.timeout(120000),
    })) output += token;
    assert(output.trim().length > 0, 'Expected real model output');
    checks.push('project lock survives runtime restart and serves real CPU inference through the CLI client');
    const post = async body => fetch(cfg.baseURL + '/chat/completions', { method: 'POST',
      headers: { Authorization: `Bearer ${cfg.apiKey}`, 'Content-Type': 'application/json', 'X-Potluck-Scope': 'local' },
      body: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }], max_tokens: 1, ...body }),
    });
    assert.equal((await post({ model: slug, potluck_model_sha256: '0'.repeat(64) })).status, 409);
    await cli(['models', 'load', 'other-fixture']);
    assert.equal((await post({ model: slug })).status, 409);
    assert.equal((await post({ model: slug, potluck_model_sha256: lock.model.sha256 })).status, 409);
    assert.equal(await fs.readFile(path.join(project, 'potluck.lock'), 'utf8'), saved);
    checks.push('wrong checksum, explicit override, and loaded model ID mismatch fail without substitution');
    await cli(['models', 'unpin']);
    assert.equal((await cli(['models', 'lock'])).status, 'unpinned');
    const after = await fs.stat(weights);
    assert.equal(after.size, before.size); assert.equal(after.mtimeMs, before.mtimeMs);
    passed = true;
  } finally {
    let stopped = false;
    try { await cli(['down']); stopped = true; } catch { /* retain private logs for diagnosis */ }
    const report = { passed: passed && stopped, stopped, checks, directory, platform: process.platform,
      architecture: process.arch, date: new Date().toISOString() };
    await fs.writeFile(path.join(directory, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
    process.stdout.write(JSON.stringify(report, null, 2) + '\n');
    if (!report.passed) process.exitCode = 1;
  }
}
main().catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
