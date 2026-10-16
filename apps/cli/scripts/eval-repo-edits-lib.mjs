import { createHash } from 'node:crypto';
import { readFile, readdir } from 'node:fs/promises';
import { join, relative } from 'node:path';

export const HARNESS_VERSION = 3;
// A broken fixture can emit many subtest failures before its completion record.
export const CHECK_MAX_BUFFER = 2 * 1024 * 1024;

export function trialCount(value, selfTest = false) {
  if (value === undefined) return selfTest ? 1 : 3;
  if (!/^[1-9][0-9]*$/.test(value) || Number(value) > 20) {
    throw new Error('--trials must be an integer from 1 to 20.');
  }
  return Number(value);
}

/** Interleave cases to keep a repeated case from occupying just one warm/cold period. */
export function planTrials(cases, count) {
  return Array.from({ length: count }, (_, index) => cases.map(fixture => ({
    id: `${fixture.id}-${index + 1}`, caseId: fixture.id, repetition: index + 1,
    status: 'not_run', passed: false, events: [], history: [],
  }))).flat();
}

const nonempty = value => typeof value === 'string' && value.trim().length > 0 && value.length <= 500;

/** Operator-supplied conditions are labeled as such, never inferred from a model name. */
export function validateEnvironment(value) {
  const keys = ['schemaVersion', 'runtimeVersion', 'backend', 'hardware', 'placement', 'transport'];
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !keys.includes(key)) || value.schemaVersion !== 1 ||
      !nonempty(value.runtimeVersion) || !nonempty(value.placement) || !nonempty(value.transport) ||
      !value.backend || typeof value.backend !== 'object' || Array.isArray(value.backend) ||
      Object.keys(value.backend).some(key => !['name', 'version'].includes(key)) ||
      !nonempty(value.backend.name) || !nonempty(value.backend.version) ||
      !Array.isArray(value.hardware) || value.hardware.length < 1 || value.hardware.length > 16 ||
      !value.hardware.every(nonempty)) {
    throw new Error('Environment must contain schemaVersion: 1, runtimeVersion, backend {name, version}, hardware [descriptions], placement, and transport.');
  }
  return structuredClone(value);
}

export async function hashes(root) {
  const result = {};
  async function walk(dir) {
    for (const item of (await readdir(dir, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
      const path = join(dir, item.name);
      if (item.isDirectory()) await walk(path);
      else if (item.isFile()) result[relative(root, path).split('\\').join('/')] =
        createHash('sha256').update(await readFile(path)).digest('hex');
      else throw new Error(`Evaluation tree contains a non-regular entry: ${path}`);
    }
  }
  await walk(root);
  return result;
}

export function fingerprint(value) {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex');
}

export async function inspectWorkspace(cwd, editable) {
  const fileHashes = await hashes(cwd);
  const emptyOrMissingSources = [];
  for (const name of editable) {
    if (!fileHashes[name] || !(await readFile(join(cwd, name), 'utf8')).trim()) emptyOrMissingSources.push(name);
  }
  return { fileHashes, emptyOrMissingSources };
}

export function assertBrokenBaseline(result, label) {
  if (!result.harnessCompleted || result.code === 0 || !/FAILED/.test(result.output) || !/AssertionError|FAIL:/.test(result.output)) {
    throw new Error(`${label} must fail assertions, not merely fail to launch: ${result.output}`);
  }
}

/** The model never sees this grade or the independent check output during a trial. */
export function gradeTrial(trial, before, after, editable, { selfTest = false } = {}) {
  const protectedChanges = Object.keys(before).filter(name =>
    !editable.includes(name) && before[name] !== after.fileHashes[name]);
  const unexpectedFiles = Object.keys(after.fileHashes).filter(name => !(name in before));
  const changedSourceFiles = editable.filter(name => before[name] !== after.fileHashes[name]);
  const failureReasons = [];
  if (trial.error) failureReasons.push('execution_error');
  if (!selfTest && trial.agentResult?.status !== 'finished') failureReasons.push(
    `agent_${trial.agentResult?.reason ?? 'did_not_finish'}`);
  if (trial.visible?.code !== 0) failureReasons.push('visible_checks_failed');
  if (trial.oracle?.code !== 0) failureReasons.push('independent_checks_failed');
  if (protectedChanges.length || unexpectedFiles.length) failureReasons.push('protected_files_changed');
  if (after.emptyOrMissingSources.length) failureReasons.push('source_deleted_or_emptied');
  if (changedSourceFiles.length !== editable.length) failureReasons.push('incomplete_cross_file_repair');
  if (!selfTest && !trial.events.some(event => event.type === 'verify' && event.ok)) {
    failureReasons.push('no_successful_automatic_verify');
  }
  return {
    changedSourceFiles, protectedChanges, unexpectedFiles,
    emptyOrMissingSources: after.emptyOrMissingSources,
    protectedFilesIntact: protectedChanges.length === 0 && unexpectedFiles.length === 0,
    failureReasons, passed: failureReasons.length === 0,
  };
}

export function trialMetrics(trial, totalDurationMs, { selfTest = false } = {}) {
  const verifications = trial.events.filter(event => event.type === 'verify');
  const relativeTime = value => value === undefined || selfTest ? null : value - (trial.agentStartedMs ?? 0);
  return {
    verificationAttempts: verifications.length,
    verificationRetries: Math.max(0, verifications.length - 1),
    failedVerifications: verifications.filter(event => !event.ok).length,
    toolFailures: trial.events.filter(event => event.type === 'toolResult' && event.result.startsWith('error:')).length,
    deniedActions: trial.events.filter(event => event.type === 'confirmation' && !event.approved).length,
    firstTokenMs: relativeTime(trial.events.find(event => event.type === 'firstToken')?.elapsedMs),
    firstVisibleVerifyPassMs: relativeTime(verifications.find(event => event.ok)?.elapsedMs),
    // Includes final visible + independent grading. Failed attempts have no verified time.
    timeToVerifiedResultMs: trial.passed ? relativeTime(totalDurationMs) : null,
  };
}

export function summarizeTrials(trials) {
  const cases = {};
  for (const trial of trials) {
    const item = cases[trial.caseId] ??= { planned: 0, passed: 0, failed: 0, notRun: 0, running: 0, verifiedTimesMs: [] };
    item.planned++;
    if (trial.status === 'not_run') item.notRun++;
    else if (trial.status === 'running') item.running++;
    else if (trial.passed) item.passed++;
    else item.failed++;
    item.verifiedTimesMs.push(trial.metrics?.timeToVerifiedResultMs ?? null);
  }
  const passed = trials.filter(trial => trial.passed).length;
  return {
    planned: trials.length, passed,
    failed: trials.filter(trial => trial.status === 'failed').length,
    notRun: trials.filter(trial => trial.status === 'not_run').length,
    running: trials.filter(trial => trial.status === 'running').length,
    // The denominator includes interrupted/unrun trials; no best-of-N selection.
    passRate: trials.length ? passed / trials.length : null,
    cases,
  };
}

export function markNotRun(trials, reason) {
  for (const trial of trials) if (trial.status === 'not_run') trial.notRunReason = reason;
}

/** A successful process exit alone can be an accidental sys.exit during import. */
export function checkResult(exitCode, output, token) {
  const prefix = `POTLUCK_EVAL_CHECK:${token}:`;
  const markers = output.split(/\r?\n/).filter(line => line.startsWith(prefix));
  let completion;
  try { if (markers.length === 1) completion = JSON.parse(markers[0].slice(prefix.length)); } catch { /* incomplete marker */ }
  const harnessCompleted = completion && ['testsRun', 'failures', 'errors', 'skipped'].every(key =>
    Number.isSafeInteger(completion[key]) && completion[key] >= 0) && completion.testsRun > 0;
  const passed = exitCode === 0 && harnessCompleted && completion.failures === 0 &&
    completion.errors === 0 && completion.skipped === 0;
  return { code: passed ? 0 : exitCode || 1, output: output.slice(-12000),
    harnessCompleted: Boolean(harnessCompleted), ...(harnessCompleted ? { completion } : {}) };
}

export function checkExecutionError(error, token) {
  if (error.code === 'ENOENT') throw error;
  const overflow = error.code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER';
  const notice = overflow ? '\nCheck exceeded its output limit.'
    : error.killed ? '\nCheck exceeded its execution limit.' : '';
  return {
    ...checkResult(typeof error.code === 'number' ? error.code : 124,
      String(error.stdout ?? '') + String(error.stderr ?? '') + notice, token),
    ...(overflow ? { failure: 'output_limit' } : {}),
  };
}

/** No host shell, home, network or writable source mount; also used for final grading. */
export function checkArguments(cwd, oracle, token) {
  const script = checkerScript('/workspace', oracle ? '/oracle' : '/workspace/tests', token);
  return [
    '--cpu=10:10', '--as=805306368:805306368', '--fsize=1048576:1048576', '--nofile=128:128',
    '--', 'bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--clearenv', '--cap-drop', 'ALL',
    '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib', '--ro-bind', '/lib64', '/lib64',
    '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
    '--ro-bind', cwd, '/workspace', '--chdir', '/workspace',
    ...(oracle ? ['--ro-bind', oracle, '/oracle'] : []),
    '--', '/usr/bin/python3', '-I', '-B', '-c', script,
  ];
}

export function checkerScript(workspace, tests, token) {
  return `import sys, unittest, json\nsys.path.insert(0, ${JSON.stringify(workspace)})\nsuite = unittest.defaultTestLoader.discover(${JSON.stringify(tests)})\nresult = unittest.TextTestRunner(verbosity=2).run(suite)\nprint(${JSON.stringify(`POTLUCK_EVAL_CHECK:${token}:`)} + json.dumps(dict(testsRun=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped))), flush=True)\nsys.exit(0 if result.wasSuccessful() and result.testsRun else 1)`;
}
