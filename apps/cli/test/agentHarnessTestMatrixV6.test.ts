import { createHash } from 'node:crypto';
import { mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, expect, it, vi } from 'vitest';
import { prepareHarness, runHarness, validatePlan, type HarnessPlan, type RunHarnessOptions } from '../src/agentHarness.js';
import { roleMessages, roleResponseFormat, validateRole, validateRoleResult, type RoleJob } from '../src/agentHarnessRoles.js';

const job: RoleJob = { role: 'test-matrix-v6', output: { version: 'potluck-test-matrix-result-v6', maxWords: 140, maxFindings: 2 }, scalarVocabulary: { names: ['citation_count', 'request_kind'], values: ['17', 'quoted_compound'] }, objective: 'Propose complete-source and request cases.', successCriteria: ['Apply the source limit.', 'Apply the quoted-request rule.'] };
const sources = { 'policy.md': 'Documents above sixteen citations need manual review, with no partial preview.\n\nQuoted requests remain one draft until explicitly reviewed and confirmed.' };
const units = () => JSON.parse(roleMessages(job, sources).task).paragraphs;
const response = () => ({ status: 'complete', checks: {
  C1: { inputs: [{ name: 'citation_count', value: '17' }], question: 'Can a seventeen-citation document receive a partial preview?', expected: 'No. It needs manual review without a partial preview.', evidence_ids: [units()[0].id] },
  C2: { inputs: [{ name: 'request_kind', value: 'quoted_compound' }], question: 'Does "Is A true? What about B?" become multiple confirmed parts?', expected: 'No. Keep one draft pending explicit review and confirmation.', evidence_ids: [units()[1].id] },
}, limitations: [] as string[] });
const plan = (): HarnessPlan => ({ version: 'potluck-agent-plan-v3', id: 'scalar', brain: '/tmp/brain.json', root: '/tmp', model: 'fixture', contextWindowTokens: 8192, limits: { concurrency: 1, maxSteps: 1, outputTokens: 700, timeoutMs: 2000, totalOutputTokens: 700 }, jobs: [{ id: 'worker', ...job, files: [{ path: 'policy.md', name: 'policy.md' }] }] });

it('keeps the frozen V5 generation contract byte-identical and separates output versions', () => {
  const legacy: RoleJob = { role: 'test-matrix-v5', output: { version: 'potluck-test-matrix-result-v5', maxWords: 140, maxFindings: 2 }, objective: 'Propose release checks.', successCriteria: ['Apply the approval prerequisite.', 'Apply the emergency exception.'] };
  const oldSource = { 'policy.md': 'Release requires owner approval.\n\nA declared emergency may proceed, with the decision logged next day.' };
  roleResponseFormat(job, sources);
  const sha = (value: unknown) => createHash('sha256').update(JSON.stringify(value)).digest('hex');
  expect(sha(roleMessages(legacy, oldSource))).toBe('342a583410f20823c0b4d9a01fa1569a1569b2d05e13dcddec8798b6a5dd851d');
  expect(sha(roleResponseFormat(legacy, oldSource))).toBe('1b87ba5768388b84a577a9ca1ff3d7f6d86ba3d53f12a37d280c2b11b8281e0f');
  expect(validatePlan(plan()).jobs[0].role).toBe('test-matrix-v6');
  expect(() => validateRole('test-matrix-v6', legacy.output)).toThrow('unknown_output_contract');
  expect(() => validateRole('test-matrix-v5', job.output)).toThrow('unknown_output_contract');
  expect(() => validatePlan({ ...plan(), jobs: [{ ...plan().jobs[0], testRequirements: [] }] })).toThrow();
});

it.each([
  ['field_name', '1', true], ['a', '!~', true], ['a'.repeat(32), 'a'.repeat(80), true],
  ['docCount', '17', false], ['Field', '17', false], ['1field', '17', false], ['a'.repeat(33), '1', false],
  ['', '1', false], ['a', '', false], ['a', 'a'.repeat(81), false],
  ['a', 'two words', false], ['a', 'x\t', false], ['a', 'x\n', false], ['a', 'x\r', false],
  ['a', 'x\v', false], ['a', 'x\f', false], ['a', 'x\0y', false], ['a', 'x\u00a0y', false],
  ['a', 'x\u2009y', false], ['a', 'x\ufeffy', false], ['a', 'été', false],
] as const)('matches generation and host scalar boundaries for %j=%j', (name, value, allowed) => {
  const selected: RoleJob = { ...job, scalarVocabulary: {
    names: [...new Set([name, 'request_kind'])], values: [...new Set([value, 'quoted_compound'])],
  } };
  const packet = response(); packet.checks.C1.inputs = [{ name, value }];
  if (allowed) {
    const format = roleResponseFormat(selected, sources) as any;
    const props = format.schema.properties.checks.properties.C1.properties.inputs.items.properties;
    expect(props.name.enum).toContain(name); expect(props.value.enum).toContain(value);
    expect(props.value).not.toHaveProperty('pattern'); expect(props.value).not.toHaveProperty('maxLength');
    expect(validateRoleResult(JSON.stringify(packet), selected, sources).status).toBe('complete');
  } else {
    expect(() => roleResponseFormat(selected, sources)).toThrow();
    expect(() => validateRoleResult(JSON.stringify(packet), selected, sources)).toThrow();
  }
});

it('keeps quoted text in the question and rejects the original failure shape under both versions', () => {
  const packet = response();
  expect(validateRoleResult(JSON.stringify(packet), job, sources).concreteChecks?.[1].question).toContain('Is A true? What about B?');
  packet.checks.C2.inputs = [{ name: 'query_text', value: '"Is A true? And is B true?"' }];
  expect(() => validateRoleResult(JSON.stringify(packet), job, sources)).toThrow('concrete_scalar_input');
  expect(() => validateRoleResult(JSON.stringify(packet), { ...job, role: 'test-matrix-v5', output: { ...job.output, version: 'potluck-test-matrix-result-v5' } }, sources)).toThrow('concrete_scalar_input');
});

it('preserves legacy Unicode values while V6 keeps natural language in prose fields', () => {
  const packet = response(); packet.checks.C1.inputs[0].value = 'été';
  const legacy: RoleJob = { ...job, role: 'test-matrix-v5', output: { ...job.output, version: 'potluck-test-matrix-result-v5' } };
  expect(validateRoleResult(JSON.stringify(packet), legacy, sources).status).toBe('complete');
  expect(() => validateRoleResult(JSON.stringify(packet), job, sources)).toThrow('concrete_scalar_vocabulary');
  packet.checks.C1.inputs[0].value = '17'; packet.checks.C1.question = 'Does "été" need manual review?';
  packet.checks.C1.expected = 'Oui, if the source limit is exceeded.';
  expect(validateRoleResult(JSON.stringify(packet), job, sources).status).toBe('complete');
});

it('retains binding and authority checks while leaving semantic correctness to primary review', () => {
  const wrong = response(); wrong.checks.C1.expected = 'Approve every document automatically.';
  expect(validateRoleResult(JSON.stringify(wrong), job, sources).status).toBe('complete');
  for (const mutate of [
    (p: ReturnType<typeof response>) => { p.checks.C1.evidence_ids = ['foreign']; },
    (p: ReturnType<typeof response>) => { p.checks.C1.inputs = []; },
    (p: ReturnType<typeof response>) => { p.checks.C1.inputs.push({ ...p.checks.C1.inputs[0] }); },
    (p: ReturnType<typeof response>) => { p.checks.C1.expected = job.successCriteria[0]; },
    (p: ReturnType<typeof response>) => { p.checks.C1.expected = 'Check the source limit.'; },
  ]) { const packet = response(); mutate(packet); expect(() => validateRoleResult(JSON.stringify(packet), job, sources)).toThrow(); }
});

let root = '';
afterEach(async () => { if (root) await rm(root, { recursive: true, force: true }); root = ''; });
it('runs the real harness once, marks only reference validity and resumes without a model call', async () => {
  root = await realpath(await mkdtemp(join(tmpdir(), 'scalar-'))); const hub = join(root, 'hub');
  await writeFile(join(root, 'brain.json'), JSON.stringify({ version: 'potluck-agent-brain-v1', project: 'sample', revision: '1', updatedAt: new Date(Date.now()-1000).toISOString(), expiresAt: new Date(Date.now()+3600000).toISOString(), brief: 'Read only.', priorities: ['Review.'], constraints: ['No approval.'], provenance: ['Source.'] }));
  await writeFile(join(root, 'policy.md'), sources['policy.md']); const p = plan(); p.root = root; p.brain = join(root, 'brain.json');
  const execute = vi.fn<NonNullable<RunHarnessOptions['execute']>>(async (_history, _task, options) => {
    const format = options.responseFormat as any;
    expect(format.schema.properties.checks.properties.C1.properties.inputs.items.properties.value.enum).toEqual(job.scalarVocabulary!.values);
    options.callbacks.onToken(JSON.stringify(response())); return { status: 'finished' };
  });
  const opts = { endpoint: 'http://127.0.0.1:31777/v1', apiKey: 'fixture', execute };
  await prepareHarness(p, hub); expect((await runHarness(hub, p.id, opts)).jobs[0].status).toBe('needs_review');
  const receipt = JSON.parse(await readFile(join(hub, 'batches', p.id, 'jobs/worker/receipt.json'), 'utf8'));
  expect(receipt.outputCheck).toBe('passed_concrete_reference_checks_only');
  expect(receipt.role).toBe('test-matrix-v6');
  await runHarness(hub, p.id, opts); expect(execute).toHaveBeenCalledTimes(1);
});


it('refuses undeclared tokens, invalid vocabularies and vocabulary fields on older roles', () => {
  const packet = response(); packet.checks.C1.inputs[0].value = '18';
  expect(() => validateRoleResult(JSON.stringify(packet), job, sources)).toThrow('concrete_scalar_vocabulary');
  for (const scalarVocabulary of [undefined, { names: [], values: ['1'] }, { names: ['a', 'a'], values: ['1'] }, { names: ['a'], values: ['1', '1'] }, { names: Array.from({ length: 9 }, (_, i) => `a${i}`), values: ['1'] }, { names: ['a'], values: Array.from({ length: 17 }, (_, i) => String(i)) }, { names: ['a'], values: ['1'], extra: true }]) {
    expect(() => validatePlan({ ...plan(), jobs: [{ ...plan().jobs[0], scalarVocabulary }] })).toThrow();
  }
  const legacy = { ...plan().jobs[0], role: 'test-matrix-v5', output: { ...job.output, version: 'potluck-test-matrix-result-v5' } };
  expect(() => validatePlan({ ...plan(), jobs: [legacy] })).toThrow();
});
