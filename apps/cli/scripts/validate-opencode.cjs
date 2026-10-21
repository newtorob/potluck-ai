// Real OpenCode against synthetic inference; this measures protocol compatibility, not model quality.
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const assert = require('node:assert/strict');
const run = promisify(execFile);

async function check(scenario) {
  assert(path.isAbsolute(process.env.POTLUCK_OPENCODE_BIN || ''), 'Set POTLUCK_OPENCODE_BIN to OpenCode 1.19.3.');
  const directory = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'potluck-opencode-validation-')));
  const project = path.join(directory, 'project'); const profile = path.join(directory, 'profile');
  await fs.mkdir(project); await fs.mkdir(profile);
  const hash = 'a'.repeat(64); const model = 'fixture-coder'; const marker = 'POTLUCK_READ_7AC2';
  const sentinel = JSON.stringify({ model: 'other/wrong', provider: { other: { options: { baseURL: 'http://127.0.0.1:1/v1' } } } });
  await fs.writeFile(path.join(project, 'README.md'), `Validation code: ${marker}\n`);
  await fs.writeFile(path.join(project, 'opencode.json'), sentinel);
  await fs.writeFile(path.join(project, 'potluck.lock'), JSON.stringify({ schemaVersion: 1, model: { id: model, format: 'gguf', sha256: hash, sizeBytes: 100 } }));
  const contextWindow = Number(process.env.POTLUCK_TEST_CONTEXT || 65536);
  const calls = []; let requestedRead = false; let requestedWrite = false; let sawReadResult = false; let rejectedFixtureError;
  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://fixture');
      assert.equal(req.headers.authorization, 'Bearer isolated-gateway-key');
      res.setHeader('Content-Type', 'application/json');
      if (url.pathname === '/v1/potluck/context') {
        assert.equal(url.searchParams.get('model'), model); assert.equal(url.searchParams.get('scope'), 'local');
        assert.equal(url.searchParams.get('potluck_model_sha256'), hash);
        res.end(JSON.stringify({ model, scope: 'local', context_window: contextWindow })); return;
      }
      assert.equal(url.pathname, '/v1/chat/completions');
      assert.equal(req.headers['x-potluck-scope'], 'local');
      let raw = ''; for await (const part of req) raw += part;
      const body = JSON.parse(raw); calls.push(body);
      assert.equal(body.model, model); assert.equal(body.potluck_model_sha256, hash);
      assert(body.max_tokens <= 4096);
      res.setHeader('X-Potluck-Model-Lock', '1'); res.setHeader('X-Potluck-Model-SHA256', hash);
      let message = { role: 'assistant', content: 'POTLUCK_TOOL_OK' }; let finish = 'stop';
      if (body.tool_choice?.function?.name === 'potluck_probe') {
        message = { role: 'assistant', content: null, tool_calls: [{ id: 'probe-call', type: 'function', function: { name: 'potluck_probe', arguments: '{"value":"POTLUCK_TOOL_OK"}' } }] };
        finish = 'tool_calls';
      } else if (body.stream) {
        const result = body.messages.find(m => m.role === 'tool' && JSON.stringify(m.content).includes(marker));
        if (result) { sawReadResult = true; message.content = marker; }
        else if (scenario === 'denied-write' && body.tools?.some(t => t.function?.name === 'write')) {
          assert(!requestedWrite, 'Unexpected write retry'); requestedWrite = true;
          message = { role: 'assistant', content: null, tool_calls: [{ id: 'write-call', type: 'function', function: { name: 'write', arguments: JSON.stringify({ filePath: path.join(project, 'README.md'), content: 'THIS MUST NOT BE WRITTEN' }) } }] };
          finish = 'tool_calls';
        } else if (body.tools?.some(t => t.function?.name === 'read')) {
          assert(!requestedRead, 'Repeated read without a tool result'); requestedRead = true;
          message = { role: 'assistant', content: null, tool_calls: [{ id: 'read-call', type: 'function', function: { name: 'read', arguments: JSON.stringify({ filePath: path.join(project, 'README.md') }) } }] };
          finish = 'tool_calls';
        } else { message.content = 'Fixture title'; }
      }
      const packet = { id: 'fixture', object: body.stream ? 'chat.completion.chunk' : 'chat.completion', created: 1, model };
      if (body.stream) {
        res.setHeader('Content-Type', 'text/event-stream');
        const delta = message.tool_calls ? { role: 'assistant', tool_calls: message.tool_calls.map((call, index) => ({ index, ...call })) } : message;
        for (const choice of [{ index: 0, delta, finish_reason: null }, { index: 0, delta: {}, finish_reason: finish }]) {
          res.write(`data: ${JSON.stringify({ ...packet, choices: [choice] })}\n\n`);
        }
        res.end('data: [DONE]\n\n');
      } else res.end(JSON.stringify({ ...packet, choices: [{ index: 0, message, finish_reason: finish }], usage: { prompt_tokens: 20, completion_tokens: 10, total_tokens: 30 } }));
    } catch (error) { rejectedFixtureError = String(error); res.statusCode = 500; res.end(JSON.stringify({ error: { message: 'Fixture validation failed' } })); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const env = { PATH: process.env.PATH, POTLUCK_OPENCODE_BIN: process.env.POTLUCK_OPENCODE_BIN,
    POTLUCK_DATA_DIR: profile, POTLUCK_GATEWAY_KEY: 'isolated-gateway-key',
    POTLUCK_GATEWAY_URL: `http://127.0.0.1:${server.address().port}/v1`,
    POTLUCK_SIDECAR_URL: 'http://127.0.0.1:1', POTLUCK_LOCAL_API_SOCKET: path.join(directory, 'absent.sock'),
    NO_COLOR: '1', OPENCODE_CONFIG_CONTENT: '{"model":"inherited/wrong"}' };
  try {
    const result = await run(process.execPath, [path.resolve(__dirname, '../dist/index.js'), 'launch', 'opencode', '--scope', 'local', '--message', scenario === 'read' ? 'Read README.md using the read tool and reply with its validation code. Do not edit files or run shell commands.' : 'Use the write tool to replace README.md with a short sentence.'], { cwd: project, env, timeout: 120000, maxBuffer: 2 * 1024 * 1024 });
    await fs.writeFile(path.join(directory, 'output.txt'), result.stdout + result.stderr, { mode: 0o600 });
    assert.equal(rejectedFixtureError, undefined);
    if (scenario === 'read') { assert(requestedRead); assert(sawReadResult); assert.match(result.stdout, new RegExp(marker)); }
    else { assert(requestedWrite); assert.match(result.stdout + result.stderr, /permission requested|rejected permission/); } assert.equal(await fs.readFile(path.join(project, 'opencode.json'), 'utf8'), sentinel);
    assert.equal(await fs.readFile(path.join(project, 'README.md'), 'utf8'), `Validation code: ${marker}\n`);
    assert(!result.stdout.includes('isolated-gateway-key') && !result.stderr.includes('isolated-gateway-key'));
    const report = { passed: true, scenario, client: 'OpenCode 1.19.3', platform: process.platform, architecture: process.arch,
      inference: 'synthetic gateway; no model/GPU/quality claim', loadedContextFixture: contextWindow, calls: calls.length,
      readToolExecuted: requestedRead, toolResultReturned: sawReadResult, writeToolOfferedButDenied: requestedWrite, projectConfigurationPreserved: true,
      maxRequestBytes: Math.max(...calls.map(body => Buffer.byteLength(JSON.stringify(body)))), directory };
    await fs.writeFile(path.join(directory, 'result.json'), JSON.stringify(report, null, 2));
    process.stdout.write(JSON.stringify(report) + '\n');
  } catch (error) {
    await fs.writeFile(path.join(directory, 'failure.txt'), String(error) + '\n' + (error.stdout || '') + (error.stderr || '') + '\n' + (rejectedFixtureError || ''), { mode: 0o600 });
    throw new Error(`OpenCode validation failed; inspect ${directory}/failure.txt`);
  } finally { await new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }); }
}
async function main() { for (const scenario of ['read', 'denied-write']) await check(scenario); }
main().catch(error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; });
