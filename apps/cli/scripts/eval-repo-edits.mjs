#!/usr/bin/env node
// Linux-only, bounded evaluation of the real CLI agent/tools/gateway/verify loop.
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cp, mkdir, mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs, promisify } from 'node:util';
import { runTask, systemPrompt } from '../dist/agent.js';
import { assertGatewayReady, loadConfig } from '../dist/config.js';
import { readProjectModelLock, validateLock } from '../dist/model-lock.js';
import { buildRepoMap } from '../dist/repomap.js';

const exec = promisify(execFile);
const { values } = parseArgs({ options: {
  'self-test': { type: 'boolean' }, out: { type: 'string' }, lock: { type: 'string' },
  case: { type: 'string' }, suite: { type: 'string', default: 'repo-edit-v1' },
} });
if (!['repo-edit-v1', 'repo-edit-transfer-v1'].includes(values.suite)) throw new Error('Unknown evaluation suite.');
const suite = resolve(dirname(fileURLToPath(import.meta.url)), '../eval', values.suite);
const manifest = JSON.parse(await readFile(join(suite, 'manifest.json'), 'utf8'));
if (process.platform !== 'linux') throw new Error('This evaluation requires Linux bubblewrap; there is no unsandboxed fallback.');
if (!values.out) throw new Error('Specify --out with a directory for synthetic traces and edited fixture copies.');
const output = resolve(values.out);
await mkdir(output, { recursive: true, mode: 0o700 });
const runDir = await mkdtemp(join(output, `${manifest.version}-`));
const verifyCommand = 'python3 -m unittest discover -s tests -v';

async function hashes(root) {
  const result = {};
  async function walk(dir) {
    for (const item of (await readdir(dir, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
      const path = join(dir, item.name);
      if (item.isDirectory()) await walk(path);
      else if (item.isFile()) result[relative(root, path)] = createHash('sha256').update(await readFile(path)).digest('hex');
      else throw new Error(`Fixture contains a non-regular entry: ${path}`);
    }
  }
  await walk(root);
  return result;
}

// Only the fixture and standard interpreter libraries are visible. The model
// cannot select a command, access host credentials, or reach the network.
async function check(cwd, oracle) {
  const script = `import sys, unittest\nsys.path.insert(0, '/workspace')\nsuite = unittest.defaultTestLoader.discover(${JSON.stringify(oracle ? '/oracle' : '/workspace/tests')})\nresult = unittest.TextTestRunner(verbosity=2).run(suite)\nsys.exit(0 if result.wasSuccessful() and result.testsRun else 1)`;
  const args = [
    '--cpu=10:10', '--as=805306368:805306368', '--fsize=1048576:1048576', '--nofile=128:128',
    '--', 'bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--clearenv', '--cap-drop', 'ALL',
    '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib', '--ro-bind', '/lib64', '/lib64',
    '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
    '--ro-bind', cwd, '/workspace', '--chdir', '/workspace',
    ...(oracle ? ['--ro-bind', oracle, '/oracle'] : []),
    '--', '/usr/bin/python3', '-I', '-B', '-c', script,
  ];
  try {
    const { stdout, stderr } = await exec('prlimit', args, { timeout: 15000, killSignal: 'SIGKILL', maxBuffer: 128 * 1024 });
    return { code: 0, output: (stdout + stderr).slice(-12000) };
  } catch (error) {
    if (error.code === 'ENOENT') throw error;
    return { code: typeof error.code === 'number' ? error.code : 124,
      output: (String(error.stdout ?? '') + String(error.stderr ?? '') + (error.killed ? '\nCheck exceeded its execution limit.' : '')).slice(-12000) };
  }
}

const fixtureHashes = await hashes(suite);
const report = {
  suite: manifest.version, fixtureSha256: createHash('sha256').update(JSON.stringify(fixtureHashes)).digest('hex'),
  fixtureHashes, startedAt: new Date().toISOString(),
  policy: { ...manifest, temperature: 0.2, maxOutputTokens: 4096, scope: 'local',
    shellTools: 'denied', verify: verifyCommand, sandbox: 'bubblewrap, no network, read-only mounts, 10s CPU / 15s wall / 768MiB per check' },
  selfTest: Boolean(values['self-test']), trials: [],
};
const save = () => writeFile(join(runDir, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
let cfg;
let lock;
if (!report.selfTest) {
  if (!values.lock) throw new Error('Specify --lock with an existing, runtime-verified potluck.lock.');
  lock = validateLock(JSON.parse(await readFile(resolve(values.lock), 'utf8')));
  cfg = await loadConfig();
  assertGatewayReady(cfg);
  const url = new URL(cfg.baseURL);
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) throw new Error('Evaluation requires a loopback gateway.');
  report.model = lock.model;
}
const selected = manifest.cases.filter(item => !values.case || item.id === values.case);
if (!selected.length) throw new Error('Unknown fixture case.');
await save();
for (const fixture of selected) {
  const root = join(suite, fixture.id);
  const cwd = join(runDir, fixture.id);
  await cp(join(root, 'project'), cwd, { recursive: true });
  const oracle = join(root, 'oracle');
  const trial = { id: fixture.id, workspace: cwd, startedAt: new Date().toISOString(), events: [], history: [] };
  report.trials.push(trial);
  const before = await hashes(cwd);
  const initial = await check(cwd);
  if (initial.code === 0 || !/FAILED/.test(initial.output)) throw new Error(`Fixture ${fixture.id} must initially fail its assertions, not fail to launch: ${initial.output}`);
  trial.initialChecks = initial;
  if (report.selfTest) {
    trial.initialOracle = await check(cwd, oracle);
    if (trial.initialOracle.code === 0 || !/FAILED/.test(trial.initialOracle.output)) throw new Error('Oracle must reject the original implementation.');
    for (const name of fixture.editable) await cp(join(root, 'reference', name), join(cwd, name));
  } else {
    await mkdir(join(cwd, '.git'));
    await writeFile(join(cwd, 'potluck.lock'), JSON.stringify(lock, null, 2));
    const modelLock = await readProjectModelLock(cwd);
    const prompt = await readFile(join(root, 'task.md'), 'utf8');
    const history = [{ role: 'system', content: systemPrompt(cwd, verifyCommand) + '\n\n' + await buildRepoMap(cwd) }];
    trial.history = history;
    const started = performance.now();
    let firstToken = true;
    const event = (type, data = {}) => trial.events.push({ type, elapsedMs: Math.round(performance.now() - started), ...data });
    try {
      await runTask(history, prompt, {
        cfg: { ...cfg, modelLock }, model: modelLock.model.id, cwd,
        scope: 'local', charBudget: manifest.charBudget, maxSteps: manifest.maxSteps,
        signal: AbortSignal.timeout(manifest.timeoutMs), verifyCommand,
        confirm: async request => {
          const approved = ['edit_file', 'write_file'].includes(request.tool) &&
            fixture.editable.some(name => resolve(cwd, name) === resolve(cwd, request.subject));
          event('confirmation', { tool: request.tool, subject: request.subject, approved });
          return approved;
        },
        runVerify: async () => check(cwd),
        callbacks: {
          onToken: () => { if (firstToken) { event('firstToken'); firstToken = false; } },
          onTurnEnd: () => event('turnEnd'),
          onAction: action => { event('action', { action }); process.stdout.write(JSON.stringify({ case: fixture.id, tool: action.tool }) + '\n'); },
          onToolResult: result => event('toolResult', { result }),
          onNotice: message => event('notice', { message }),
          onVerify: (command, ok, output) => event('verify', { command, ok, output }),
        },
      });
    } catch (error) { trial.error = error.message; }
    trial.durationMs = Math.round(performance.now() - started);
    // No model output or final prose is used to decide correctness.
  }
  trial.visible = await check(cwd);
  trial.oracle = await check(cwd, oracle);
  const after = await hashes(cwd);
  trial.changedSourceFiles = fixture.editable.filter(name => before[name] !== after[name]);
  trial.protectedFilesIntact = Object.keys(before).filter(name => !fixture.editable.includes(name)).every(name => before[name] === after[name]);
  trial.passed = !trial.error && trial.visible.code === 0 && trial.oracle.code === 0 &&
    trial.protectedFilesIntact && trial.changedSourceFiles.length === fixture.editable.length &&
    !trial.events.some(e => e.type === 'notice') &&
    (report.selfTest || trial.events.some(e => e.type === 'verify' && e.ok));
  await save();
  process.stdout.write(JSON.stringify({ case: fixture.id, passed: trial.passed, durationMs: trial.durationMs,
    error: trial.error, edits: trial.changedSourceFiles, report: join(runDir, 'report.json') }) + '\n');
  if (trial.error) break; // A broken transport may have stopped a managed split.
}
report.finishedAt = new Date().toISOString();
report.passed = report.trials.length === selected.length && report.trials.every(t => t.passed);
await save();
process.stdout.write(JSON.stringify({ passed: report.passed, report: join(runDir, 'report.json') }) + '\n');
process.exitCode = report.passed ? 0 : 1;
