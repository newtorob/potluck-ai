#!/usr/bin/env node
'use strict';
// Exercise the installed Aider through the real CLI, with a private fake gateway.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { execFile } = require('node:child_process');
const run = require('node:util').promisify(execFile);
async function main() {
  assert(process.env.POTLUCK_AIDER_BIN, 'Set POTLUCK_AIDER_BIN to an installed Aider executable.');
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'potluck-aider-check-'));
  const project = path.join(directory, 'project');
  const home = path.join(directory, 'profile');
  await fs.mkdir(project); await fs.mkdir(home);
  await run('git', ['init', '--quiet'], { cwd: project });
  const hash = 'a'.repeat(64), calls = [];
  const server = http.createServer(async (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/v1/potluck/capabilities') return res.end(JSON.stringify({ model_lock_version: 1, scopes: ['local', 'mine'], artifact_format: 'gguf' }));
    if (req.url !== '/v1/chat/completions') { res.statusCode = 404; return res.end('{}'); }
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); calls.push({ body, headers: req.headers });
    res.setHeader('X-Potluck-Model-Lock', '1'); res.setHeader('X-Potluck-Model-SHA256', hash);
    if (body.stream) {
      res.setHeader('Content-Type', 'text/event-stream');
      for (const delta of [{ role: 'assistant', content: 'POTLUCK_AIDER_OK' }, {}]) {
        res.write(`data: ${JSON.stringify({ id: 'test', object: 'chat.completion.chunk', created: 1, model: 'fixture', choices: [{ index: 0, delta, finish_reason: delta.content ? null : 'stop' }] })}\n\n`);
      }
      res.end('data: [DONE]\n\n');
    } else res.end(JSON.stringify({ id: 'test', object: 'chat.completion', created: 1, model: 'fixture', choices: [{ index: 0, message: { role: 'assistant', content: 'POTLUCK_AIDER_OK' }, finish_reason: 'stop' }], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const existingEnv = 'OPENAI_API_BASE=http://127.0.0.1:1/v1\nOPENAI_API_KEY=wrong-existing-key\n';
  const existingConfig = 'model: openai/wrong\nweak-model: openai/wrong\nopenai-api-base: http://127.0.0.1:1/v1\n';
  await fs.writeFile(path.join(project, '.env'), existingEnv);
  await fs.writeFile(path.join(project, '.aider.conf.yml'), existingConfig);
  await fs.writeFile(path.join(project, 'potluck.lock'), JSON.stringify({ schemaVersion: 1, model: { id: 'fixture', format: 'gguf', sha256: hash, sizeBytes: 100 } }));
  const env = { ...process.env, HOME: home, POTLUCK_DATA_DIR: directory,
    POTLUCK_GATEWAY_KEY: 'real-gateway-secret', POTLUCK_GATEWAY_URL: `http://127.0.0.1:${server.address().port}/v1`,
    NO_COLOR: '1', AIDER_NO_SUGGEST_SHELL_COMMANDS: 'true', AIDER_DARK_MODE: 'false' };
  try {
    const result = await run(process.execPath, [path.resolve(__dirname, '../dist/index.js'), 'launch', 'aider', '--scope', 'local', '--message', 'Reply only POTLUCK_AIDER_OK. Do not edit any files.'], { cwd: project, env, timeout: 90000, maxBuffer: 1048576 });
    await fs.writeFile(path.join(directory, 'output.txt'), result.stdout + result.stderr, { mode: 0o600 });
    assert.match(result.stdout, /POTLUCK_AIDER_OK/);
    assert(calls.length >= 2, 'Expected readiness probe and an actual Aider request.');
    for (const { body, headers } of calls) {
      assert.equal(body.model, 'fixture'); assert.equal(body.potluck_model_sha256, hash);
      assert.equal(headers['x-potluck-scope'], 'local'); assert.equal(headers.authorization, 'Bearer real-gateway-secret');
    }
    assert.equal(await fs.readFile(path.join(project, '.env'), 'utf8'), existingEnv);
    assert.equal(await fs.readFile(path.join(project, '.aider.conf.yml'), 'utf8'), existingConfig);
    assert(!result.stdout.includes('real-gateway-secret') && !result.stderr.includes('real-gateway-secret'));
    process.stdout.write(JSON.stringify({ passed: true, requests: calls.length, directory }) + '\n');
  } catch (error) {
    await fs.writeFile(path.join(directory, 'failure.txt'), String(error) + '\n' + (error.stdout || '') + (error.stderr || ''), { mode: 0o600 });
    throw new Error(`Aider validation failed; inspect ${directory}/failure.txt`);
  } finally { await new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }); }
}
main().catch(error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; });
