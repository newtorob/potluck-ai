import { test, expect, type Page } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { mockBackends, ADMIN } from './mock';

const output = resolve(process.env.POTLUCK_E2E_OUTPUT_DIR ?? 'test-results/investigations');
const sourceId = 'source:chunk:revision';
const claimId = 'claim:reviewed';
const source = { kind: 'source_chunk', citation_id: sourceId, content: 'The worker was restarted.',
  presented_content: 'The worker was restarted.', relative_path: 'incident.md', revision_id: 'revision', anchor: 'worker' };
const original = { facts: [{ text: 'The worker was restarted.', citation_ids: [claimId],
  source_excerpts: [{ citation_id: sourceId, quote: 'The worker was restarted.' }] }],
  prior_fixes: [], hypotheses: [], recommended_checks: [], model_uncertainties: ['Who approved the restart?'] };
const draft = { id: 'new', workspace: 'workspace', collection_ids: ['docs'], issue: 'Worker incident',
  model_slug: 'local-model', status: 'draft', brief: original, evidence: [{ kind: 'reviewed_claim',
    citation_id: claimId, content: 'Worker restart recorded.', presented_content: 'Worker restart recorded.', sources: [source] }],
  retrieval_notices: ['Evidence context was truncated; inspect sources directly.'],
  provenance: { schema_version: 2, model_sha256: 'abc', template_version: 'investigation-v4' },
  review: null, outcome: null, created_at: '2026-10-29T12:00:00Z' };
const legacy = { ...draft, id: 'old', issue: 'Historical incident', evidence: [source], retrieval_notices: [],
  provenance: {}, brief: { facts: [], prior_fixes: [], hypotheses: [], recommended_checks: [],
    missing_evidence: ['Old combined notice'] } };
const snippet = { snippet_id: 'S1', citation_id: sourceId, quote: 'The worker was restarted.' };
const answerDraft = { ...draft, brief: { questions: [{ id: 'Q1', text: 'What happened?' }],
  answers: [{ question_id: 'Q1', status: 'supported', text: 'The worker restarted.', snippet_ids: ['S1'], source_excerpts: [snippet] }],
  facts: [], prior_fixes: [], hypotheses: [], recommended_checks: [], model_uncertainties: [] },
  provenance: { ...draft.provenance, template_version: 'investigation-v6', question_origin: 'explicit', snippet_catalog: [snippet] } };
const calculationDraft = { ...answerDraft, brief: { ...answerDraft.brief, questions: [{ id: 'R1', text: 'How much changed?' }],
  answers: [{ question_id: 'R1', status: 'unknown', text: 'Unknown: source value could not be extracted.', snippet_ids: ['S1'], source_excerpts: [snippet] }] },
  provenance: { ...answerDraft.provenance, template_version: 'investigation-v9',
    extraction_notices: [{ snippet_id: 'S1', citation_id: sourceId, source_text: '20–30%', reason: 'Range is unsupported' }],
    calculation_traces: [{ request_id: 'R1', operation: 'difference', status: 'unknown', formula: 'after - before', reason: 'Missing input', caveat: 'Arithmetic does not verify source selection or authority.', inputs: [{ id: 'N1', citation_id: sourceId, source_text: '20%' }], algorithm_version: 'cited-calculations-v2', normalized_inputs: [], rounding: { mode: 'ROUND_HALF_UP', max_fractional_digits: 6 } }] } };

type Controls = { unavailable: boolean; empty: boolean; failCreate: boolean; createGate?: Promise<void>; planGate?: Promise<void>; failPlan?: boolean };
async function setup(page: Page, controls: Controls) {
  const appOrigin = new URL(test.info().project.use.baseURL as string).origin;
  await page.route('**/*', (route) => {
    if (new URL(route.request().url()).origin === appOrigin) return route.continue();
    return route.abort();
  });
  await mockBackends(page, { me: ADMIN });
  let current: Record<string, unknown> = structuredClone(draft);
  await page.route(/:8321\//, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const headers = { 'access-control-allow-origin': '*', 'access-control-allow-methods': 'GET,POST,OPTIONS',
      'access-control-allow-headers': '*' };
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    const fulfill = (json: unknown, status = 200) => route.fulfill({ json, status, headers });
    if (path === '/models') return fulfill([]);
    if (path === '/health') return fulfill({ status: 'ok', model_loaded: true, active_model_id: 'local-model' });
    if (path === '/company-brain/collections') return fulfill({ collections: controls.empty ? [] : [
      { id: 'docs', name: 'Incident docs', scan_status: 'ready', last_scan_at: '2026-10-29T12:00:00Z' }] });
    if (path === '/company-brain/investigations/plan') {
      if (controls.planGate) await controls.planGate;
      if (controls.failPlan) return fulfill({ detail: 'Planning failed' }, 400);
      return fulfill({ operations: [{ request: 'How much changed?', operation: 'difference' }], model_slug: 'local-model', model_sha256: 'abc', generation_profile: null, notices: [] });
    }
    if (path === '/company-brain/investigations' && request.method() === 'POST') {
      if (controls.createGate) await controls.createGate;
      if (controls.failCreate) return fulfill({ detail: 'generation_failed: unavailable' }, 400);
      if (request.postDataJSON().answer_each_question) current = structuredClone(answerDraft);
      if (request.postDataJSON().reviewed_operations) current = structuredClone(calculationDraft);
      return fulfill(current);
    }
    if (path === '/company-brain/investigations') return fulfill({ investigations: [current, legacy] });
    if (path === '/company-brain/investigations/old') return fulfill(legacy);
    if (path === '/company-brain/investigations/new') return fulfill(current);
    if (path === '/company-brain/investigations/new/review') {
      if (controls.unavailable) return fulfill({ detail: 'investigation_unavailable: source changed' }, 404);
      const input = request.postDataJSON();
      if (input.corrected_brief.questions) {
        if (input.corrected_brief.answers[0].text === 'Invalid') return fulfill({ detail: 'invalid_review_brief: Invalid corrected brief' }, 400);
        current = { ...current, status: 'reviewed', review: { decision: 'correct', reviewer: input.reviewer,
          reason: input.reason, corrected_brief: { ...input.corrected_brief, answers: input.corrected_brief.answers.map((a: Record<string, unknown>) => ({ ...a, source_excerpts: (a.snippet_ids as string[]).map(() => snippet) })) } } };
        return fulfill(current);
      }
      if (input.corrected_brief.facts[0].source_excerpts[0].quote === 'Forged excerpt')
        return fulfill({ detail: 'invalid_review_brief: Invalid corrected brief' }, 400);
      current = { ...draft, status: 'reviewed', review: { decision: 'correct', reviewer: input.reviewer,
        reason: input.reason, corrected_brief: input.corrected_brief } };
      return fulfill(current);
    }
    return fulfill([]);
  });
  await page.setViewportSize({ width: 1152, height: 768 });
  await mkdir(output, { recursive: true });
  await page.goto('/#/investigations');
}

test('reviewed plan can be edited and retained across failures before a cited calculation draft', async ({ page }) => {
  let release!: () => void;
  const controls: Controls = { unavailable: false, empty: false, failCreate: false, planGate: new Promise<void>((resolve) => { release = resolve; }) };
  await setup(page, controls);
  await page.setViewportSize({ width: 480, height: 740 });
  await scope(page);
  await page.getByLabel('Issue', { exact: true }).fill('Worker incident');
  await page.getByLabel('Evidence query').fill('worker');
  await page.getByLabel('Loaded local model').selectOption('local-model');
  await page.getByLabel('Reviewed calculations (experimental)').check();
  const requestField = page.getByLabel('Request 1');
  await expect(requestField).toHaveAttribute('aria-invalid', 'true');
  const suggest = page.getByRole('button', { name: 'Suggest a plan' });
  const suggestWidth = (await suggest.boundingBox())!.width;
  await suggest.click();
  const busySuggest = page.getByRole('button', { name: 'Suggesting…' });
  await expect(busySuggest).toBeDisabled();
  expect((await busySuggest.boundingBox())!.width).toBe(suggestWidth);
  release();
  await expect(requestField).toHaveValue('How much changed?');
  const select = page.getByLabel('Calculation type 1');
  await select.focus(); await page.keyboard.press('Space'); await page.keyboard.press('Escape');
  await expect(select).toBeFocused();
  await page.getByRole('button', { name: 'Add request' }).click();
  await page.getByLabel('Request 2').fill('How much changed?');
  await expect(requestField).toHaveAttribute('aria-invalid', 'true');
  await expect(page.getByLabel('Request 2')).toHaveAccessibleDescription('Use a distinct request.');
  await page.getByRole('button', { name: 'Remove request 2' }).click();
  await requestField.fill('Edited change request');
  await page.setViewportSize({ width: 1152, height: 768 });
  await requestField.evaluate((el) => el.scrollIntoView({ block: 'center' }));
  await page.screenshot({ path: `${output}/reviewed-plan-desktop.png`, animations: 'disabled' });
  await page.setViewportSize({ width: 480, height: 740 });
  await requestField.evaluate((el) => el.scrollIntoView({ block: 'center' }));
  await page.screenshot({ path: `${output}/reviewed-plan-narrow.png`, animations: 'disabled' });
  controls.failPlan = true;
  await suggest.click();
  await expect(page.getByRole('alert')).toContainText('Planning failed');
  await expect(requestField).toHaveValue('Edited change request');
  controls.failCreate = true;
  let releaseCreate!: () => void;
  controls.createGate = new Promise<void>((resolve) => { releaseCreate = resolve; });
  const confirm = page.getByRole('button', { name: 'Confirm plan and draft' });
  const confirmWidth = (await confirm.boundingBox())!.width;
  await confirm.click();
  const working = page.getByRole('button', { name: 'Working…' });
  await expect(working).toBeDisabled();
  expect((await working.boundingBox())!.width).toBe(confirmWidth);
  releaseCreate();
  await expect(page.getByRole('alert')).toContainText('generation_failed');
  await expect(requestField).toHaveValue('Edited change request');
  controls.failCreate = false;
  const createRequest = page.waitForRequest((r) => r.url().endsWith('/company-brain/investigations') && r.method() === 'POST');
  await page.getByRole('button', { name: 'Confirm plan and draft' }).click();
  expect((await createRequest).postDataJSON()).toMatchObject({ plan_confirmed: true, reviewed_operations: [{ request: 'Edited change request', operation: 'difference' }] });
  await expect(page.getByText('Range is unsupported', { exact: false })).toBeVisible();
  await expect(page.getByText(/Status: unknown · Formula: after - before/)).toBeVisible();
  await expect(page.getByText(/Input N1: 20%/)).toBeVisible();
  await page.setViewportSize({ width: 1152, height: 768 });
  await page.getByRole('heading', { name: 'Original machine calculations' }).evaluate((el) => el.scrollIntoView({ block: 'start' }));
  await page.screenshot({ path: `${output}/reviewed-calculation-desktop.png`, animations: 'disabled' });
  await page.getByText('Calculation details').click();
  await expect(page.getByText('Algorithm: cited-calculations-v2')).toBeVisible();
  await page.setViewportSize({ width: 480, height: 740 });
  await page.getByRole('heading', { name: 'Original machine calculations' }).evaluate((el) => el.scrollIntoView({ block: 'start' }));
  await page.screenshot({ path: `${output}/reviewed-calculation-narrow.png`, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
});

test('question preview renders sources and recovers a correction', async ({ page }) => {
  await setup(page, { unavailable: false, empty: false, failCreate: false });
  await page.setViewportSize({ width: 480, height: 740 });
  await scope(page);
  await page.getByLabel('Answer each question (preview)').check();
  await page.getByLabel('Questions, one per line (optional)').fill('What happened?');
  await page.getByLabel('Answer profile').selectOption('qwen3-non-thinking');
  await page.setViewportSize({ width: 1152, height: 768 });
  await page.getByLabel('Answer profile').scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/question-profile-desktop.png`, animations: 'disabled' });
  await page.setViewportSize({ width: 480, height: 740 });
  const request = page.waitForRequest((r) => r.url().endsWith('/company-brain/investigations') && r.method() === 'POST');
  await create(page);
  expect((await request).postDataJSON()).toMatchObject({ answer_each_question: true, questions: ['What happened?'], generation_profile: 'qwen3-non-thinking' });
  await expect(page.getByText('Supported (model claim)', { exact: true })).toBeVisible();
  await expect(page.getByText(/Draft answers need review/)).toBeVisible();
  await page.setViewportSize({ width: 1152, height: 768 });
  await page.getByRole('heading', { name: 'Questions and answers' }).evaluate((el) => el.scrollIntoView({ block: 'start' }));
  await page.screenshot({ path: `${output}/question-answer-desktop.png`, animations: 'disabled' });
  await page.setViewportSize({ width: 480, height: 740 });
  await page.getByRole('link', { name: 'Source 1.1' }).click();
  await expect(page.locator('#investigation-source-1-1')).toBeInViewport();
  await page.getByLabel('Decision').selectOption('correct');
  await page.getByLabel('Answer text').fill('Invalid');
  await page.getByLabel('Reviewer', { exact: true }).fill('Owner');
  await page.getByLabel('Reason', { exact: true }).fill('Checked source');
  await page.getByRole('button', { name: 'Save review' }).click();
  await expect(page.getByRole('alert')).toContainText('Check the correction');
  await expect(page.getByLabel('Answer text')).toHaveValue('Invalid');
  await page.getByLabel('Answer text').fill('Corrected answer');
  await page.getByRole('button', { name: 'Save review' }).click();
  await expect(page.getByText('Corrected answer')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
});
async function scope(page: Page) {
  await page.getByLabel('Workspace', { exact: true }).fill('workspace');
  await page.getByRole('button', { name: 'Load collections' }).click();
  await page.getByLabel(/Incident docs/).check();
}
async function create(page: Page) {
  await page.getByLabel('Issue', { exact: true }).fill('Worker incident');
  await page.getByLabel('Evidence query').fill('worker');
  await page.getByLabel('Loaded local model').selectOption('local-model');
  await page.getByRole('button', { name: 'Create investigation' }).click();
}
async function correction(page: Page) {
  await page.getByLabel('Decision').selectOption('correct');
  await page.getByLabel('Reviewer', { exact: true }).fill('Owner');
  await page.getByLabel('Reason', { exact: true }).fill('Checked source');
  await page.getByRole('textbox', { name: 'Item 1' }).fill('Corrected worker finding');
  await page.getByLabel('Exact excerpt from source').fill('Forged excerpt');
}

test('new brief citations, recoverable correction, review, and legacy draft', async ({ page }) => {
  const controls: Controls = { unavailable: false, empty: false, failCreate: false };
  await setup(page, controls);
  await scope(page);
  await create(page);
  await expect(page.getByText('Who approved the restart?')).toBeVisible();
  await expect(page.getByText('Evidence context was truncated; inspect sources directly.')).toBeVisible();
  await page.getByRole('link', { name: 'View citation 1' }).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#investigation-source-1-1')).toContainText('source:chunk:revision');
  await page.getByRole('link', { name: 'Source 1.1' }).click();
  await expect(page.locator('#investigation-source-1-1')).toContainText('The worker was restarted.');
  await expect(page.locator('#investigation-source-1-1')).toBeInViewport();
  expect(await page.evaluate(() => document.scrollingElement?.scrollTop)).toBe(0);
  await expect(page.getByRole('heading', { name: 'Investigations', exact: true })).toBeInViewport();
  await page.getByRole('heading', { name: 'Worker incident', exact: true }).evaluate((el) => el.scrollIntoView({ block: 'start' }));
  await expect(page.getByText('Who approved the restart?')).toBeInViewport();
  await page.screenshot({ path: `${output}/potluck-grounding-brief-dark.png`, animations: 'disabled' });
  const decision = page.getByLabel('Decision');
  await decision.focus();
  await page.keyboard.press('Space');
  await page.keyboard.press('Escape');
  await expect(decision).toBeFocused();
  await expect(decision).toHaveValue('accept');
  await correction(page);
  await page.getByRole('button', { name: 'Save review' }).click();
  await expect(page.getByRole('alert')).toContainText('Check the correction');
  await expect(page.getByRole('textbox', { name: 'Item 1' })).toHaveValue('Corrected worker finding');
  await expect(page.getByLabel('Exact excerpt from source')).toHaveValue('Forged excerpt');
  await page.getByLabel('Exact excerpt from source').fill('The worker was restarted.');
  await page.getByRole('button', { name: 'Save review' }).click();
  await expect(page.getByText('Review saved.')).toBeVisible();
  await expect(page.getByText('Corrected worker finding')).toBeVisible();
  await page.getByRole('button', { name: 'Historical incident · draft' }).click();
  await expect(page.getByText('Historical combined missing evidence (provenance unknown)')).toBeVisible();
  await expect(page.getByText('Old combined notice')).toBeVisible();
  await page.getByRole('button', { name: 'Switch to Common Room (light)' }).click();
  await expect(page.locator('html')).toHaveClass(/light/);
  await page.getByRole('heading', { name: 'Historical incident', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/potluck-grounding-legacy-light.png`, animations: 'disabled' });
});

test('unavailable evidence clears the draft; loading, errors and narrow layout', async ({ page }) => {
  let release!: () => void;
  const controls: Controls = { unavailable: true, empty: false, failCreate: false,
    createGate: new Promise<void>((resolve) => { release = resolve; }) };
  await setup(page, controls);
  await page.setViewportSize({ width: 480, height: 740 });
  await scope(page);
  await create(page);
  await expect(page.getByRole('button', { name: 'Working…' })).toBeDisabled();
  release();
  await expect(page.getByRole('heading', { name: 'Worker incident', exact: true })).toBeVisible();
  await correction(page);
  await page.getByRole('button', { name: 'Save review' }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Worker incident', exact: true })).toHaveCount(0);
  controls.failCreate = true;
  await page.getByRole('button', { name: 'Create investigation' }).click();
  await expect(page.getByRole('alert')).toContainText('generation_failed');
  controls.empty = true;
  await page.getByRole('button', { name: 'Load collections' }).click();
  await expect(page.getByText('No collections registered.', { exact: false })).toBeVisible();
  expect((await page.getByLabel('Workspace', { exact: true }).boundingBox())!.width).toBeGreaterThan(100);
  expect((await page.locator('label').filter({ has: page.getByLabel('Workspace', { exact: true }) }).boundingBox())!.height).toBeLessThan(80);
  expect(await page.evaluate(() => ({ doc: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    main: document.querySelector('main')!.scrollWidth <= document.querySelector('main')!.clientWidth + 1,
    top: document.scrollingElement?.scrollTop }))).toEqual({ doc: true, main: true, top: 0 });
  await page.screenshot({ path: `${output}/potluck-grounding-empty-narrow.png`, animations: 'disabled' });
});
