#!/usr/bin/env node
'use strict';

// Uses existing GGUF weights read-only via a symlink in private temporary state.
// Run after building the CLI. Does not connect the mesh or modify installed data.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const run = promisify(execFile);
const ROOT = path.resolve(__dirname, '../../..');

async function main() {
  assert.notEqual(process.platform, 'win32');
  const weights = process.env.POTLUCK_VALIDATION_MODEL_FILE;
  assert(weights && path.isAbsolute(weights), 'Set POTLUCK_VALIDATION_MODEL_FILE to an existing GGUF file.');
  const before = await fs.stat(weights);
  assert(before.isFile());
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'potluck-model-restore-'));
  await fs.chmod(directory, 0o700);
  const listener = net.createServer();
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const slug = 'restore-fixture';
  const folder = path.join(directory, 'models', slug);
  await fs.mkdir(folder, { recursive: true });
  await fs.symlink(weights, path.join(folder, 'model.gguf'));
  await fs.writeFile(path.join(directory, 'config.json'), JSON.stringify({
    runtime: { context_size: 512, gpu_layers: 0, threads: 2, max_tokens: 16 },
    network: { contributor_mode: false },
  }), { mode: 0o600 });
  const env = { ...process.env, POTLUCK_DATA_DIR: directory,
    POTLUCK_CLOUD_API_URL: 'http://127.0.0.1:9',
    POTLUCK_CLOUD_API_BASE: 'http://127.0.0.1:9',
    POTLUCK_SIDECAR_URL: `http://127.0.0.1:${port}`,
    POTLUCK_GATEWAY_URL: `http://127.0.0.1:${port}/v1`,
    POTLUCK_BIND_HOST: '127.0.0.1', POTLUCK_LOCAL_API_SOCKET: path.join(directory, 'absent.sock'), NO_COLOR: '1' };
  for (const key of ['POTLUCK_GATEWAY_KEY', 'POTLUCK_SIDECAR_TOKEN', 'POTLUCK_MEMORY_DB_PATH', 'POTLUCK_VERIFY_MODEL_ON_LOAD']) delete env[key];
  const read = async file => JSON.parse(await fs.readFile(path.join(directory, file), 'utf8'));
  const cli = async args => {
    let result;
    try {
      result = await run(process.execPath, [path.join(ROOT, 'apps/cli/dist/index.js'), ...args, '--json', '--timeout', '30000'],
        { env, timeout: 45000, maxBuffer: 2 ** 20 });
    } catch {
      throw new Error(`CLI ${args.join(' ')} failed; inspect ${directory}/logs/runtime.log`);
    }
    return JSON.parse(result.stdout);
  };
  const api = async (route, options = {}) => {
    const record = await read('run/runtime.json');
    const response = await fetch(record.url + route, {
      ...options, headers: { Authorization: `Bearer ${record.token}`, 'Content-Type': 'application/json', ...options.headers },
      signal: AbortSignal.timeout(60000),
    });
    assert(response.ok, `${route} returned HTTP ${response.status}`);
    return response.json();
  };
  const waitFor = async status => {
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      const health = await api('/health');
      if (health.model_restore.status === status) return health;
      assert.notEqual(health.model_restore.status, 'failed', 'Model restore failed; inspect private runtime log.');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error(`Timed out waiting for restore state ${status}.`);
  };
  const restart = async () => { await cli(['down']); await cli(['up']); };
  const checks = [];
  let passed = false;
  try {
    await cli(['setup', '--no-input', '--enable-gateway']);
    await cli(['models', 'load', slug]);
    assert.equal((await cli(['models', 'autoload'])).model, slug);
    await restart();
    const restored = await waitFor('restored');
    assert.equal(restored.active_model_id, slug);
    assert.equal(restored.n_ctx_loaded, 512);
    assert.equal(restored.loaded_rpc_servers, null);
    const settings = await read('config.json');
    const completion = await api('/v1/chat/completions', { method: 'POST',
      headers: { Authorization: `Bearer ${settings.gateway.api_key}` },
      body: JSON.stringify({ model: slug, messages: [{ role: 'user', content: 'Reply with just the digit: what is 2+2? /no_think' }], max_tokens: 64, temperature: 0 }),
    });
    assert(completion.choices[0].message.content.includes('4'));
    checks.push('local model persisted across process restart and served real CPU inference');

    await cli(['models', 'autoload', 'off']);
    await restart();
    assert.equal((await waitFor('disabled')).model_loaded, false);
    assert.equal((await cli(['models', 'autoload'])).model, slug);
    await cli(['models', 'autoload', 'on']);
    await restart();
    assert.equal((await waitFor('restored')).active_model_id, slug);
    checks.push('startup opt-out skips loading without forgetting the saved choice; opt-in restores it');

    await cli(['models', 'unload', slug]);
    await restart();
    assert.equal((await api('/health')).model_loaded, false);
    assert.equal((await cli(['models', 'autoload'])).model, null);
    checks.push('explicit unload stays unloaded after restart');

    await cli(['models', 'load', slug]);
    await api(`/models/${slug}`, { method: 'DELETE' });
    await restart();
    assert.equal((await api('/health')).model_loaded, false);
    assert.equal((await cli(['models', 'autoload'])).model, null);
    checks.push('deleting the loaded model clears restoration without touching original weights');

    await api('/settings', { method: 'PATCH', body: JSON.stringify({ runtime: { startup_model_id: 'missing-fixture' } }) });
    await restart();
    assert.equal((await waitFor('failed')).model_loaded, false);
    await cli(['models', 'unload', 'missing-fixture']);
    assert.equal((await cli(['models', 'autoload'])).model, null);
    checks.push('missing saved weights fail softly and can be cleared through the CLI');
    const after = await fs.stat(weights);
    assert.equal(after.size, before.size);
    assert.equal(after.mtimeMs, before.mtimeMs);
    passed = true;
  } finally {
    let stopped = false;
    try { await cli(['down']); stopped = true; } catch { /* report retained for diagnosis */ }
    const report = { passed: passed && stopped, stopped, checks, directory,
      runtime: env.POTLUCK_RUNTIME_BIN || 'source', platform: process.platform, date: new Date().toISOString() };
    await fs.writeFile(path.join(directory, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
    process.stdout.write(JSON.stringify(report, null, 2) + '\n');
    if (!report.passed) process.exitCode = 1;
  }
}
main().catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
