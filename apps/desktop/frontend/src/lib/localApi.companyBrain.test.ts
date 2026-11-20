import { afterAll, beforeAll, beforeEach, expect, it, vi } from 'vitest';

vi.mock('@/stores/connectionStore', () => ({ coordinatorBase: () => 'http://coordinator.test' }));
vi.mock('@/lib/coordinatorAuth', () => ({ coordinatorSessionRequest: vi.fn(), detailOf: vi.fn() }));
const fetchMock = vi.fn();
let localApi: (typeof import('./localApi'))['localApi'];
beforeAll(async () => {
  vi.stubGlobal('fetch', fetchMock);
  ({ localApi } = await import('./localApi'));
});
beforeEach(() =>
  fetchMock
    .mockReset()
    .mockImplementation(
      async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
    ),
);
afterAll(() => vi.unstubAllGlobals());

it('requests heading-preserving scans only through the confirmed reviewed-pilot route', async () => {
  const input = { workspace: 'workspace', scan_confirmed: true as const };
  await localApi.scanPilotCollection('collection/one', input);
  expect(fetchMock).toHaveBeenCalledOnce();
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe('http://127.0.0.1:8321/company-brain/pilot/collections/collection%2Fone/scan');
  expect(JSON.parse(String(init.body))).toEqual({ ...input, preserve_heading_context: true });
  expect(input).toEqual({ workspace: 'workspace', scan_confirmed: true });
});

it('requests organization without changing the reviewed question, parts, source scope or confirmation', async () => {
  const input = {
    workspace: 'workspace',
    collection_ids: ['one', 'two'],
    question: 'What is allowed, and when?',
    model_slug: 'loaded-local-model',
    pilot_confirmed: true as const,
    question_parts: ['What is allowed?', 'When?'],
    question_parts_confirmed: true as const,
  };
  await localApi.askReviewedPilot(input);
  expect(fetchMock).toHaveBeenCalledOnce();
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe('http://127.0.0.1:8321/company-brain/pilot/ask-reviewed');
  expect(JSON.parse(String(init.body))).toEqual({ ...input, organize_answer: true });
  expect(input).not.toHaveProperty('organize_answer');
});

it('retains explicit caller opt-outs for both optional capabilities', async () => {
  await localApi.scanPilotCollection('one', {
    workspace: 'workspace',
    scan_confirmed: true,
    preserve_heading_context: false,
  });
  expect(JSON.parse(String(fetchMock.mock.calls[0]![1].body)).preserve_heading_context).toBe(false);
  await localApi.askReviewedPilot({
    workspace: 'workspace',
    collection_ids: ['one'],
    question: 'What is allowed?',
    model_slug: 'loaded-local-model',
    pilot_confirmed: true,
    question_parts: ['What is allowed?'],
    question_parts_confirmed: true,
    organize_answer: false,
  });
  expect(JSON.parse(String(fetchMock.mock.calls[1]![1].body)).organize_answer).toBe(false);
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it('requests a direct current source draft with the explicit workspace and selected-folder scope only', async () => {
  const input = {
    workspace: 'w',
    collection_ids: ['one', 'two'],
    collection_id: 'two',
    relative_path: 'Policies/release.md',
  };
  await localApi.pilotSourceDraft(input);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe('http://127.0.0.1:8321/company-brain/pilot/source-draft');
  expect(JSON.parse(String(init.body))).toEqual(input);
  expect(init.method).toBe('POST');
  expect(fetchMock).toHaveBeenCalledOnce();
});

it.each(['v37', 'v38', 'v39', 'v40', 'v41', 'v42', 'v43', 'v47'] as const)(
  'keeps %s on exact source presentation without changing earlier organization defaults',
  async (version) => {
    const input = {
      workspace: 'workspace',
      collection_ids: ['one'],
      question: 'Which rule?',
      model_slug: 'loaded-local-model',
      pilot_confirmed: true as const,
      question_parts: ['Which rule?'],
      question_parts_confirmed: true as const,
      reviewed_answer_version: version,
    };
    await localApi.askReviewedPilot(input);
    expect(JSON.parse(String(fetchMock.mock.calls[0]![1].body))).toEqual({
      ...input,
      organize_answer: false,
    });
    await localApi.askReviewedPilot({ ...input, organize_answer: true });
    expect(JSON.parse(String(fetchMock.mock.calls[1]![1].body)).organize_answer).toBe(true);
    expect(input).not.toHaveProperty('organize_answer');
  },
);
