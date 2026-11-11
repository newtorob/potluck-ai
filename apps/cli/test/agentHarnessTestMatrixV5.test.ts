import { mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, expect, it, vi } from 'vitest';
import { validatePlan, prepareHarness, runHarness, type HarnessPlan, type RunHarnessOptions } from '../src/agentHarness.js';
import { roleMessages, roleResponseFormat, validateRoleResult, type RoleJob } from '../src/agentHarnessRoles.js';

const job: RoleJob = { role: 'test-matrix-v5', output: { version: 'potluck-test-matrix-result-v5', maxWords: 140, maxFindings: 2 }, objective: 'Propose applied deadline and deletion cases.', successCriteria: ['Apply the deadline anchor.', 'Apply the deletion exception.'] };
const sources = { '@brain': 'Read-only context.', 'policy.md': 'Purge payloads within 30 days after fulfillment.\n\nDeleting a tenant retains request payloads pending a separate purge.' };
const units = () => JSON.parse(roleMessages(job, sources).task).paragraphs;
const response = () => ({ status: 'complete', checks: {
  C1: { inputs: [{ name: 'fulfilled', value: '2026-01-01' }], question: 'When is the payload purge due?', expected: 'By January 31, 2026.', evidence_ids: [units()[0].id] },
  C2: { inputs: [{ name: 'tenant_deleted', value: 'true' }], question: 'Does tenant deletion remove request payloads?', expected: 'No. Request payloads remain pending a separate purge.', evidence_ids: [units()[1].id] },
}, limitations: [] as string[] });
const plan = (): HarnessPlan => ({ version: 'potluck-agent-plan-v3', id: 'applied', brain: '/tmp/brain.json', root: '/tmp', model: 'fixture', contextWindowTokens: 8192, limits: { concurrency: 1, maxSteps: 1, outputTokens: 700, timeoutMs: 2000, totalOutputTokens: 700 }, jobs: [{ id: 'worker', ...job, files: [{ path: 'policy.md', name: 'policy.md' }] }] });

it('binds applied cases to host-copied evidence without certifying their answers', () => {
  expect(validatePlan(plan()).jobs[0].role).toBe('test-matrix-v5');
  const result = validateRoleResult(JSON.stringify(response()), job, sources);
  expect(result.concreteChecks?.[0].inputs[0].value).toBe('2026-01-01');
  expect(result.findings[1].evidence[0].quote).toBe(sources['policy.md'].split('\n\n')[1]);
  expect(result.summary).toContain('requiring primary review');
  const wrong = response(); wrong.checks.C1.expected = 'By February 28, 2026.';
  expect(validateRoleResult(JSON.stringify(wrong), job, sources).status).toBe('complete');
  // Reference validity leaves arithmetic and source application to primary review.
  expect(JSON.parse(roleMessages(job, sources).task)).not.toHaveProperty('testRequirements');
  const format: any = roleResponseFormat(job, sources);
  expect(format.schema.properties.checks.required).toEqual(['C1', 'C2']);
  expect(format.schema.properties.checks.properties.C1.properties.evidence_ids.items.enum).toEqual(units().map((u: any) => u.id));
  expect(() => validatePlan({ ...plan(), jobs: [{ ...plan().jobs[0], testRequirements: [] }] })).toThrow();
});

it('rejects copied instructions, unapplied cases and unbound references', () => {
  const bad: Array<(v: ReturnType<typeof response>) => void> = [
    v => { v.checks.C1.inputs = []; },
    v => { v.checks.C1.inputs[0].value = 'after fulfillment'; },
    v => { v.checks.C1.inputs.push({ ...v.checks.C1.inputs[0] }); },
    v => { v.checks.C1.question = 'When is it due'; },
    v => { v.checks.C1.question = job.objective; },
    v => { v.checks.C1.expected = job.successCriteria[0]; },
    v => { v.checks.C1.expected = 'Catch a clock started at deletion.'; },
    v => { v.checks.C1.expected = 'The answer should verify the deadline.'; },
    v => { v.checks.C1.evidence_ids = []; },
    v => { v.checks.C1.evidence_ids = ['foreign']; },
    v => { v.checks.C1.evidence_ids.push(v.checks.C1.evidence_ids[0]); },
    v => { (v.checks as any).C3 = v.checks.C2; },
    v => { delete (v.checks as any).C2; },
    v => { v.checks.C1.expected = 'word '.repeat(100); v.checks.C2.expected = 'word '.repeat(100); },
  ];
  for (const mutate of bad) { const value = response(); mutate(value); expect(() => validateRoleResult(JSON.stringify(value), job, sources)).toThrow(); }
  expect(() => validateRoleResult('{"status":"complete","status":"complete"}', job, sources)).toThrow();
  const echo = { status: 'complete', checks: { C1: { criterion: 'C1', setup: 'Request fulfilled after deletion.', action: 'Check the deadline anchor.', expected: 'Catch a clock started at deletion.', evidence_ids: [units()[0].id] } }, limitations: [] };
  expect(() => validateRoleResult(JSON.stringify(echo), job, sources)).toThrow();
});

it('permits explicit unresolved cases only with empty inputs, no evidence and a limitation', () => {
  const value = response(); value.status = 'insufficient';
  value.checks.C1 = { inputs: [], question: 'Unresolved', expected: 'Unresolved', evidence_ids: [] };
  value.limitations = ['The source does not provide this rule.'];
  expect(validateRoleResult(JSON.stringify(value), job, sources).status).toBe('insufficient');
  value.limitations = []; expect(() => validateRoleResult(JSON.stringify(value), job, sources)).toThrow();
  value.limitations = ['Missing rule.']; value.checks.C1.inputs = [{ name: 'value', value: '1' }];
  expect(() => validateRoleResult(JSON.stringify(value), job, sources)).toThrow();
});

let root = '';
afterEach(async () => { if (root) await rm(root, { recursive: true, force: true }); root = ''; });
it('runs the real harness once, labels reference-only checks and resumes without inference', async () => {
  root = await realpath(await mkdtemp(join(tmpdir(), 'concrete-'))); const hub = join(root, 'hub');
  await writeFile(join(root, 'brain.json'), JSON.stringify({ version: 'potluck-agent-brain-v1', project: 'sample', revision: '1', updatedAt: new Date(Date.now()-1000).toISOString(), expiresAt: new Date(Date.now()+3600000).toISOString(), brief: 'Context.', priorities: ['Review.'], constraints: ['Read only.'], provenance: ['Source.'] }));
  await writeFile(join(root, 'policy.md'), sources['policy.md']); const p = plan(); p.root = root; p.brain = join(root, 'brain.json');
  const execute = vi.fn<NonNullable<RunHarnessOptions['execute']>>(async (_history, task, options) => {
    expect(JSON.parse(task)).not.toHaveProperty('testRequirements');
    options.callbacks.onToken(JSON.stringify(response())); return { status: 'finished' };
  });
  const opts = { endpoint: 'http://127.0.0.1:31777/v1', apiKey: 'fixture', execute };
  await prepareHarness(p, hub); expect((await runHarness(hub, p.id, opts)).jobs[0].status).toBe('needs_review');
  const receipt = JSON.parse(await readFile(join(hub, 'batches', p.id, 'jobs/worker/receipt.json'), 'utf8'));
  expect(receipt.outputCheck).toBe('passed_concrete_reference_checks_only');
  await runHarness(hub, p.id, opts); expect(execute).toHaveBeenCalledTimes(1);
});
