#!/usr/bin/env node
// Linux-only, bounded evaluation of the real CLI agent/tools/gateway/verify loop.
import { execFile } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { appendFileSync } from 'node:fs';
import { cp, mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { arch, cpus, platform, release, totalmem } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs, promisify } from 'node:util';
import { runTask, systemPrompt } from '../dist/agent.js';
import { prepareContext } from '../dist/context.js';
import { assertGatewayReady, loadConfig } from '../dist/config.js';
import { modelContextWindow } from '../dist/gateway.js';
import { readProjectModelLock, validateLock } from '../dist/model-lock.js';
import { buildRepoMap } from '../dist/repomap.js';
import {
  HARNESS_VERSION, CHECK_MAX_BUFFER, assertBrokenBaseline, checkArguments, checkExecutionError, checkResult, fingerprint, gradeTrial,
  hashes, inspectWorkspace, markNotRun, planTrials, summarizeTrials, trialCount,
  trialMetrics, validateEnvironment,
} from './eval-repo-edits-lib.mjs';

const exec = promisify(execFile);
const { values } = parseArgs({ options: {
  'self-test': { type: 'boolean' }, out: { type: 'string' }, lock: { type: 'string' },
  case: { type: 'string' }, suite: { type: 'string', default: 'repo-edit-v1' },
  trials: { type: 'string' }, environment: { type: 'string' },
} });
if (!['repo-edit-v1', 'repo-edit-transfer-v1'].includes(values.suite)) throw new Error('Unknown evaluation suite.');
const cliRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const suite = join(cliRoot, 'eval', values.suite);
const manifest = JSON.parse(await readFile(join(suite, 'manifest.json'), 'utf8'));
if (process.platform !== 'linux') throw new Error('This evaluation requires Linux bubblewrap; there is no unsandboxed fallback.');
if (!values.out) throw new Error('Specify --out with a directory for synthetic traces and edited fixture copies.');
const count = trialCount(values.trials, Boolean(values['self-test']));
const selected = manifest.cases.filter(item => !values.case || item.id === values.case);
if (!selected.length) throw new Error('Unknown fixture case.');
const output = resolve(values.out);
await mkdir(output, { recursive: true, mode: 0o700 });
const runDir = await mkdtemp(join(output, `${manifest.version}-`));
const verifyCommand = 'python3 -m unittest discover -s tests -v';
const outputTokens = prepareContext([], { contextWindowTokens: manifest.context }).maxOutputTokens;

async function check(cwd, oracle) {
  const token = randomUUID();
  try {
    const { stdout, stderr } = await exec('prlimit', checkArguments(cwd, oracle, token), {
      timeout: 15000, killSignal: 'SIGKILL', maxBuffer: CHECK_MAX_BUFFER,
    });
    return checkResult(0, stdout + stderr, token);
  } catch (error) {
    return checkExecutionError(error, token);
  }
}

// Documentation edits are not fixture changes; task, test, reference and manifest bytes are.
const fixtureHashes = Object.fromEntries(Object.entries(await hashes(suite)).filter(([path]) => !path.endsWith('README.md')));
const distHashes = Object.fromEntries(Object.entries(await hashes(join(cliRoot, 'dist'))).filter(([path]) => path.endsWith('.js')));
const runnerHashes = {};
for (const name of ['eval-repo-edits.mjs', 'eval-repo-edits-lib.mjs']) {
  runnerHashes[name] = createHash('sha256').update(await readFile(join(cliRoot, 'scripts', name))).digest('hex');
}
let revision = null;
let workingTreeDirty = null;
try { revision = (await exec('git', ['rev-parse', 'HEAD'], { cwd: cliRoot })).stdout.trim(); } catch { /* packaged checkout */ }
try { workingTreeDirty = Boolean((await exec('git', ['status', '--porcelain', '--untracked-files=normal', '--', 'src', 'scripts'], { cwd: cliRoot })).stdout.trim()); } catch { /* packaged checkout */ }
const report = {
  schemaVersion: 2, harnessVersion: HARNESS_VERSION, suite: manifest.version,
  fixtureSha256: fingerprint(fixtureHashes), fixtureHashes, startedAt: new Date().toISOString(),
  implementation: { revision, workingTreeDirty, runnerHashes, distSha256: fingerprint(distHashes), distHashes },
  host: { platform: platform(), release: release(), arch: arch(), node: process.version,
    cpuModels: [...new Set(cpus().map(cpu => cpu.model))], memoryBytes: totalmem() },
  policy: { ...manifest, repetitions: count, temperature: 0.2, maxOutputTokens: outputTokens, maxVerifyFailures: 4, scope: 'local',
    contextSizing: 'conservative UTF-8 byte estimate with output and template room reserved',
    shellTools: 'denied', verify: verifyCommand, independentChecks: 'after agent finishes; never used for repair or candidate selection',
    sandbox: 'bubblewrap, no network, read-only mounts, 10s CPU / 15s wall / 768MiB per check',
    checkOutputLimitBytes: CHECK_MAX_BUFFER },
  selfTest: Boolean(values['self-test']), trials: planTrials(selected, count),
};
const save = async () => {
  report.summary = summarizeTrials(report.trials);
  await writeFile(join(runDir, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
};
await save();

let cfg;
let lock;
try {
  report.checkerEnvironment = {};
  for (const [name, command, args] of [
    ['python', '/usr/bin/python3', ['-I', '-B', '--version']],
    ['bubblewrap', 'bwrap', ['--version']],
    ['prlimit', 'prlimit', ['--version']],
  ]) {
    const { stdout, stderr } = await exec(command, args, { timeout: 5000, maxBuffer: 4096 });
    report.checkerEnvironment[name] = (stdout + stderr).trim();
  }
  if (!report.selfTest) {
    if (!values.lock) throw new Error('Specify --lock with an existing, runtime-verified potluck.lock.');
    if (!values.environment) throw new Error('Specify --environment with the runtime/backend/hardware conditions JSON.');
    lock = validateLock(JSON.parse(await readFile(resolve(values.lock), 'utf8')));
    report.environment = { provenance: 'operator-reported; not independently verified',
      conditions: validateEnvironment(JSON.parse(await readFile(resolve(values.environment), 'utf8'))) };
    cfg = await loadConfig();
    assertGatewayReady(cfg);
    const url = new URL(cfg.baseURL);
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) throw new Error('Evaluation requires a loopback gateway.');
    report.model = lock.model;
    report.route = { scope: 'local', gateway: cfg.baseURL, modelPin: 'SHA-256 enforced by gateway before inference' };
  }

  for (const trial of report.trials) {
    const fixture = selected.find(item => item.id === trial.caseId);
    const root = join(suite, fixture.id);
    const cwd = join(runDir, trial.id);
    const oracle = join(root, 'oracle');
    trial.workspace = cwd;
    trial.startedAt = new Date().toISOString();
    trial.status = 'running';
    const started = performance.now();
    const elapsed = () => Math.round(performance.now() - started);
    const event = (type, data = {}) => {
      const entry = { type, elapsedMs: elapsed(), ...data };
      trial.events.push(entry);
      appendFileSync(join(runDir, 'events.jsonl'), JSON.stringify({ trial: trial.id, ...entry }) + '\n', { mode: 0o600 });
    };
    let before;
    try {
      await cp(join(root, 'project'), cwd, { recursive: true });
      if (!report.selfTest) {
        await mkdir(join(cwd, '.git'));
        await writeFile(join(cwd, 'potluck.lock'), JSON.stringify(lock, null, 2));
      }
      before = await hashes(cwd);
      trial.initialChecks = await check(cwd);
      assertBrokenBaseline(trial.initialChecks, `Fixture ${fixture.id}`);
      if (report.selfTest) {
        trial.initialOracle = await check(cwd, oracle);
        assertBrokenBaseline(trial.initialOracle, `Independent checks for ${fixture.id}`);
        for (const name of fixture.editable) await cp(join(root, 'reference', name), join(cwd, name));
      } else {
        const modelLock = await readProjectModelLock(cwd);
        const runConfig = { ...cfg, modelLock };
        const context = await modelContextWindow(runConfig, modelLock.model.id, 'local');
        trial.runtimeObservation = { provenance: 'gateway-reported at trial start; exact model pin checked',
          observedAt: new Date().toISOString(), model: modelLock.model.id,
          sha256: modelLock.model.sha256, scope: 'local', contextWindowTokens: context ?? null };
        if (context !== manifest.context) throw new Error(
          `Frozen evaluation requires ${manifest.context} loaded context tokens; gateway reported ${context ?? 'unknown'}.`);
        const prompt = await readFile(join(root, 'task.md'), 'utf8');
        const history = [{ role: 'system', content: systemPrompt(cwd, verifyCommand) }];
        const repoMap = await buildRepoMap(cwd, 200, 512);
        if (repoMap) history.push({ role: 'system', content: repoMap });
        trial.history = history;
        let firstToken = true;
        trial.agentStartedMs = elapsed();
        await save();
        try { trial.agentResult = await runTask(history, prompt, {
          cfg: runConfig, model: modelLock.model.id, cwd,
          scope: 'local', contextWindowTokens: context, maxOutputTokens: outputTokens,
          charBudget: manifest.charBudget, maxSteps: manifest.maxSteps, maxVerifyFailures: 4,
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
            onAction: action => { event('action', { action }); process.stdout.write(JSON.stringify({ trial: trial.id, tool: action.tool }) + '\n'); },
            onToolResult: result => event('toolResult', { result }),
            onNotice: message => event('notice', { message }),
            onVerify: (command, ok, output) => event('verify', { command, ok, output }),
          },
        }); } catch (error) { trial.error = error.message; }
        trial.agentDurationMs = elapsed() - trial.agentStartedMs;
      }
      // These results never enter model history, including in later repetitions.
      trial.visible = await check(cwd);
      trial.oracle = await check(cwd, oracle);
      const after = await inspectWorkspace(cwd, fixture.editable);
      Object.assign(trial, gradeTrial(trial, before, after, fixture.editable, { selfTest: report.selfTest }));
    } catch (error) {
      trial.error = error.message;
      trial.failureReasons = ['execution_error'];
      trial.passed = false;
    }
    trial.durationMs = elapsed();
    trial.status = trial.passed ? 'passed' : 'failed';
    trial.metrics = trialMetrics(trial, trial.durationMs, { selfTest: report.selfTest });
    if (trial.error) markNotRun(report.trials, `Stopped after ${trial.id}: an execution or runtime error requires operator review.`);
    await save();
    process.stdout.write(JSON.stringify({ trial: trial.id, passed: trial.passed, durationMs: trial.durationMs,
      error: trial.error, edits: trial.changedSourceFiles, report: join(runDir, 'report.json') }) + '\n');
    if (trial.error) break; // A broken transport may have stopped a managed split.
  }
} catch (error) {
  report.error = error.message;
  markNotRun(report.trials, `Preflight failed: ${error.message}`);
}
report.finishedAt = new Date().toISOString();
report.passed = !report.error && report.trials.every(trial => trial.passed);
await save();
process.stdout.write(JSON.stringify({ passed: report.passed, summary: report.summary, report: join(runDir, 'report.json') }) + '\n');
process.exitCode = report.passed ? 0 : 1;
