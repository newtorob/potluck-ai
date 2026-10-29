import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { InvestigationsView } from './InvestigationsView';

const api = vi.hoisted(() => ({
  sourceCollections: vi.fn(),
  investigations: vi.fn(),
  createInvestigation: vi.fn(),
  planInvestigation: vi.fn(),
  reviewInvestigation: vi.fn(),
  previewCalculationReview: vi.fn(),
  recordInvestigationOutcome: vi.fn(),
}));
vi.mock('@/lib/localApi', () => ({ localApi: api }));
vi.mock('@/stores/modelStore', () => ({
  useModelStore: (selector: (state: unknown) => unknown) =>
    selector({
      activeModelSlug: 'local-model',
      fetchModels: async () => undefined,
    }),
}));

describe('InvestigationsView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  it('previews selected source quantities, invalidates edits, and saves the exact calculation answer', async () => {
    const answer = {
      question_id: 'R1',
      status: 'supported',
      text: '10',
      snippet_ids: ['S1'],
      source_excerpts: [{ snippet_id: 'S1', citation_id: 'ref1', quote: '10 and 20' }],
      calculation: { request_id: 'R1', operation: 'difference', status: 'computed', result: '10' },
    };
    const item = {
      id: 'i1',
      issue: 'Revenue',
      status: 'draft',
      model_slug: 'local-model',
      created_at: 'today',
      review: null,
      outcome: null,
      evidence: [{ citation_id: 'ref1', content: '10 and 20' }],
      brief: {
        questions: [{ id: 'R1', text: 'Change?' }],
        answers: [{ question_id: 'R1', status: 'unknown', text: 'Unknown', snippet_ids: [] }],
        facts: [],
        prior_fixes: [],
        hypotheses: [],
        recommended_checks: [],
      },
      provenance: {
        template_version: 'investigation-v9',
        operation_plan: [{ id: 'R1', request: 'Change?', operation: 'difference' }],
        quantity_catalog: [
          { id: 'N1', snippet_id: 'S1', source_text: '10' },
          { id: 'N2', snippet_id: 'S9', source_text: '20' },
        ],
        snippet_catalog: Array.from({ length: 9 }, (_, i) => ({
          snippet_id: `S${i + 1}`,
          citation_id: 'ref1',
          quote: i === 0 ? '10 and 20' : `Source ${i + 1}`,
        })),
        calculation_traces: [{ request_id: 'R1', operation: 'difference', status: 'unknown' }],
      },
    };
    api.sourceCollections.mockResolvedValue({
      collections: [{ id: 'c1', name: 'Runbooks', scan_status: 'ready', last_scan_at: null }],
    });
    api.investigations.mockResolvedValue({ investigations: [] });
    api.createInvestigation.mockResolvedValue(item);
    api.previewCalculationReview.mockResolvedValue({ answers: [answer], calculation_reviews: [] });
    api.reviewInvestigation.mockResolvedValue({ ...item, status: 'reviewed' });
    render(<InvestigationsView />);
    fireEvent.change(screen.getByLabelText('Workspace'), { target: { value: 'w' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load collections' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: /Runbooks/ }));
    fireEvent.change(screen.getByLabelText('Issue'), { target: { value: 'Revenue' } });
    fireEvent.change(screen.getByLabelText('Evidence query'), { target: { value: 'revenue' } });
    fireEvent.change(screen.getByLabelText('Loaded local model'), {
      target: { value: 'local-model' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create investigation' }));
    await screen.findByText('Original machine calculations');
    fireEvent.change(screen.getByLabelText('Decision'), { target: { value: 'correct' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /Review calculation inputs/ }));
    expect(screen.getByLabelText('Before')).toHaveAccessibleDescription(
      'Choose both quantities to calculate.',
    );
    expect(screen.getByLabelText(/Explanation, including/)).toHaveAccessibleDescription(
      'Enter an explanation.',
    );
    fireEvent.change(screen.getByLabelText('Before'), { target: { value: 'N1' } });
    fireEvent.change(screen.getByLabelText('After'), { target: { value: 'N1' } });
    expect(screen.getByLabelText('After')).toHaveAccessibleDescription(
      'Choose two different quantities.',
    );
    fireEvent.change(screen.getByLabelText('After'), { target: { value: 'N2' } });
    for (let i = 1; i <= 8; i++)
      fireEvent.click(
        screen.getAllByRole('checkbox', {
          name: i === 1 ? /S1 · 10 and 20/ : new RegExp(`S${i} · Source ${i}`),
        })[0]!,
      );
    expect(
      screen.getByRole('group', { name: 'Evidence and authority excerpts' }),
    ).toHaveAccessibleDescription('Use at most eight excerpts, including the selected quantities.');
    expect(screen.getByRole('button', { name: 'Preview calculation' })).toBeDisabled();
    for (let i = 2; i <= 8; i++)
      fireEvent.click(
        screen.getAllByRole('checkbox', { name: new RegExp(`S${i} · Source ${i}`) })[0]!,
      );
    fireEvent.change(screen.getByLabelText(/Explanation, including/), {
      target: { value: 'Reviewed the report' },
    });
    fireEvent.change(screen.getByLabelText('Reviewer'), { target: { value: 'Rob' } });
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Corrected' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview calculation' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Preview applied');
    expect(screen.getByLabelText('Answer text')).toHaveValue('10');
    expect(screen.getByRole('status')).toHaveTextContent('Change? · supported: 10');
    fireEvent.change(screen.getByLabelText(/Explanation, including/), {
      target: { value: 'Checked authority' },
    });
    expect(screen.getByRole('button', { name: 'Save review' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Preview calculation' }));
    await screen.findByRole('status');
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    await waitFor(() =>
      expect(api.reviewInvestigation).toHaveBeenCalledWith(
        'i1',
        expect.objectContaining({
          calculation_reviews: [
            {
              request_id: 'R1',
              status: 'calculate',
              operand_ids: ['N1', 'N2'],
              snippet_ids: ['S1'],
              reason: 'Checked authority',
            },
          ],
          corrected_brief: expect.objectContaining({ answers: [answer] }),
        }),
      ),
    );
  });
  it('discards stale suggestions and sends only an explicitly confirmed edited plan', async () => {
    api.sourceCollections.mockResolvedValue({
      collections: [{ id: 'c1', name: 'Runbooks', scan_status: 'ready', last_scan_at: null }],
    });
    api.investigations.mockResolvedValue({ investigations: [] });
    let resolvePlan!: (value: unknown) => void;
    api.planInvestigation.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePlan = resolve;
      }),
    );
    api.createInvestigation.mockRejectedValueOnce(new Error('Generation failed'));
    render(<InvestigationsView />);
    fireEvent.change(screen.getByLabelText('Workspace'), { target: { value: 'w' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load collections' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: /Runbooks/ }));
    fireEvent.change(screen.getByLabelText('Issue'), { target: { value: 'First issue' } });
    fireEvent.change(screen.getByLabelText('Evidence query'), { target: { value: 'revenue' } });
    fireEvent.change(screen.getByLabelText('Loaded local model'), {
      target: { value: 'local-model' },
    });
    fireEvent.click(screen.getByRole('checkbox', { name: /Reviewed calculations/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Suggest a plan' }));
    expect(api.planInvestigation).toHaveBeenCalledWith({
      workspace: 'w',
      collection_ids: ['c1'],
      issue: 'First issue',
      model_slug: 'local-model',
    });
    fireEvent.change(screen.getByLabelText('Issue'), { target: { value: 'Revised issue' } });
    fireEvent.change(screen.getByLabelText('Request 1'), { target: { value: 'Revenue change' } });
    resolvePlan({
      operations: [{ request: 'Stale suggestion', operation: 'answer' }],
      notices: [],
    });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Suggest a plan' })).toBeEnabled(),
    );
    expect(screen.getByLabelText('Request 1')).toHaveValue('Revenue change');
    api.planInvestigation.mockRejectedValueOnce(new Error('Suggestion failed'));
    fireEvent.click(screen.getByRole('button', { name: 'Suggest a plan' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Suggestion failed');
    expect(screen.getByLabelText('Request 1')).toHaveValue('Revenue change');
    fireEvent.change(screen.getByLabelText('Calculation type 1'), {
      target: { value: 'percent_change' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm plan and draft' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Generation failed');
    expect(screen.getByLabelText('Request 1')).toHaveValue('Revenue change');
    expect(api.createInvestigation).toHaveBeenCalledWith({
      workspace: 'w',
      collection_ids: ['c1'],
      issue: 'Revised issue',
      query: 'revenue',
      model_slug: 'local-model',
      reviewed_operations: [{ request: 'Revenue change', operation: 'percent_change' }],
      plan_confirmed: true,
    });
  });
  it('shows suggested operations, calculation trace and cited extraction notice', async () => {
    api.sourceCollections.mockResolvedValue({
      collections: [{ id: 'c1', name: 'Runbooks', scan_status: 'ready', last_scan_at: null }],
    });
    api.investigations.mockResolvedValue({ investigations: [] });
    api.planInvestigation.mockResolvedValue({
      operations: [{ request: 'What changed?', operation: 'difference' }],
      notices: ['Review this plan.'],
    });
    api.createInvestigation.mockResolvedValue({
      id: 'i1',
      issue: 'Revenue',
      status: 'draft',
      model_slug: 'local-model',
      created_at: 'today',
      review: null,
      outcome: null,
      evidence: [{ citation_id: 'ref1', content: '$10' }],
      brief: {
        questions: [{ id: 'R1', text: 'What changed?' }],
        answers: [
          {
            question_id: 'R1',
            status: 'supported',
            text: 'Increased',
            snippet_ids: ['S1'],
            source_excerpts: [{ snippet_id: 'S1', citation_id: 'ref1', quote: '$10' }],
          },
        ],
        facts: [],
        prior_fixes: [],
        hypotheses: [],
        recommended_checks: [],
        model_uncertainties: [],
      },
      provenance: {
        snippet_catalog: [{ snippet_id: 'S1', citation_id: 'ref1', quote: '$10' }],
        extraction_notices: [
          { snippet_id: 'S1', citation_id: 'ref1', source_text: '$10', reason: 'Check value' },
        ],
        calculation_traces: [
          {
            request_id: 'R1',
            operation: 'difference',
            status: 'computed',
            formula: 'after - before',
            result: '5',
            unit: 'USD',
            inputs: [{ id: 'N1', citation_id: 'ref1', source_text: '$10' }],
          },
        ],
      },
    });
    render(<InvestigationsView />);
    fireEvent.change(screen.getByLabelText('Workspace'), { target: { value: 'w' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load collections' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: /Runbooks/ }));
    fireEvent.change(screen.getByLabelText('Issue'), { target: { value: 'Revenue' } });
    fireEvent.change(screen.getByLabelText('Evidence query'), { target: { value: 'revenue' } });
    fireEvent.change(screen.getByLabelText('Loaded local model'), {
      target: { value: 'local-model' },
    });
    fireEvent.click(screen.getByRole('checkbox', { name: /Reviewed calculations/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Suggest a plan' }));
    expect(await screen.findByText('Review this plan.')).toBeInTheDocument();
    expect(screen.getByLabelText('Request 1')).toHaveValue('What changed?');
    expect(screen.getByLabelText('Calculation type 1')).toHaveValue('difference');
    fireEvent.change(screen.getByLabelText('Issue'), { target: { value: 'Revised revenue' } });
    expect(screen.getByLabelText('Request 1')).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Suggest a plan' }));
    await waitFor(() => expect(screen.getByLabelText('Request 1')).toHaveValue('What changed?'));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm plan and draft' }));
    expect(await screen.findByText(/Formula: after - before/)).toBeInTheDocument();
    expect(screen.getByText(/Check value/)).toBeInTheDocument();
    expect(screen.getByText(/Input N1/)).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'Source 1' }).length).toBeGreaterThan(1);
    api.reviewInvestigation.mockRejectedValueOnce(
      new Error('[localApi] 400: invalid_review_brief: invalid'),
    );
    fireEvent.change(screen.getByLabelText('Decision'), { target: { value: 'correct' } });
    fireEvent.change(screen.getByLabelText('Answer text'), { target: { value: 'Correction' } });
    fireEvent.change(screen.getByLabelText('Reviewer'), { target: { value: 'Sam' } });
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Review' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    await screen.findByRole('alert');
    expect(screen.getByLabelText('Answer text')).toHaveValue('Correction');
    expect(screen.getByText(/Formula: after - before/)).toBeInTheDocument();
    expect(api.reviewInvestigation).toHaveBeenCalledWith(
      'i1',
      expect.objectContaining({
        corrected_brief: expect.objectContaining({
          answers: [
            { question_id: 'R1', status: 'supported', text: 'Correction', snippet_ids: ['S1'] },
          ],
        }),
      }),
    );
  });
  it('omits the standard profile and blank questions from a preview request', async () => {
    api.sourceCollections.mockResolvedValue({
      collections: [{ id: 'c1', name: 'Runbooks', scan_status: 'ready', last_scan_at: null }],
    });
    api.investigations.mockResolvedValue({ investigations: [] });
    api.createInvestigation.mockRejectedValueOnce(new Error('Unavailable'));
    render(<InvestigationsView />);
    fireEvent.change(screen.getByLabelText('Workspace'), { target: { value: 'w' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load collections' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: /Runbooks/ }));
    fireEvent.change(screen.getByLabelText('Issue'), { target: { value: 'Failure' } });
    fireEvent.change(screen.getByLabelText('Evidence query'), { target: { value: 'failure' } });
    fireEvent.change(screen.getByLabelText('Loaded local model'), {
      target: { value: 'local-model' },
    });
    fireEvent.click(screen.getByRole('checkbox', { name: /Answer each question/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Create investigation' }));
    await screen.findByRole('alert');
    expect(api.createInvestigation).toHaveBeenCalledWith({
      workspace: 'w',
      collection_ids: ['c1'],
      issue: 'Failure',
      query: 'failure',
      model_slug: 'local-model',
      answer_each_question: true,
    });
  });
  it('creates and corrects preview answers with authoritative snippet references', async () => {
    api.sourceCollections.mockResolvedValue({
      collections: [{ id: 'c1', name: 'Runbooks', scan_status: 'ready', last_scan_at: null }],
    });
    api.investigations.mockResolvedValue({ investigations: [] });
    const snippet = { snippet_id: 'S1', citation_id: 'ref1', quote: 'Evidence text' };
    const item = {
      id: 'i1',
      issue: 'Failure',
      status: 'draft',
      model_slug: 'local-model',
      created_at: 'today',
      evidence: [{ citation_id: 'ref1', kind: 'source', content: 'Evidence text' }],
      provenance: { question_origin: 'explicit', snippet_catalog: [snippet] },
      review: null,
      outcome: null,
      brief: {
        questions: [{ id: 'Q1', text: 'Why?' }],
        answers: [
          {
            question_id: 'Q1',
            status: 'supported',
            text: 'Initial',
            snippet_ids: ['S1'],
            source_excerpts: [snippet],
          },
        ],
        facts: [],
        prior_fixes: [],
        hypotheses: [],
        recommended_checks: [],
        model_uncertainties: [],
      },
    };
    api.createInvestigation.mockResolvedValue(item);
    let rejectReview!: (error: Error) => void;
    api.reviewInvestigation.mockReturnValueOnce(
      new Promise((_, reject) => {
        rejectReview = reject;
      }),
    );
    api.reviewInvestigation.mockResolvedValue({ ...item, status: 'reviewed' });
    render(<InvestigationsView />);
    fireEvent.change(screen.getByLabelText('Workspace'), { target: { value: 'w' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load collections' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: /Runbooks/ }));
    fireEvent.change(screen.getByLabelText('Issue'), { target: { value: 'Failure' } });
    fireEvent.change(screen.getByLabelText('Evidence query'), { target: { value: 'failure' } });
    fireEvent.change(screen.getByLabelText('Loaded local model'), {
      target: { value: 'local-model' },
    });
    fireEvent.click(screen.getByRole('checkbox', { name: /Answer each question/ }));
    expect(screen.getByLabelText('Answer profile')).toHaveValue('');
    expect(screen.getByText(/requires a compatible loaded Qwen3 model/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Answer profile'), {
      target: { value: 'qwen3-non-thinking' },
    });
    fireEvent.change(screen.getByLabelText(/Questions, one per line/), {
      target: { value: 'Why?\n' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create investigation' }));
    await screen.findByText('Initial');
    expect(api.createInvestigation).toHaveBeenCalledWith(
      expect.objectContaining({
        answer_each_question: true,
        questions: ['Why?'],
        generation_profile: 'qwen3-non-thinking',
      }),
    );
    expect(screen.getByText(/not verified correctness/)).toBeInTheDocument();
    expect(screen.getByText(/presented evidence does not establish an answer/)).toBeInTheDocument();
    expect(
      screen.queryByRole('heading', { name: 'Model questions (unverified)' }),
    ).not.toBeInTheDocument();
    expect(screen.getByText('“Evidence text”')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Decision'), { target: { value: 'correct' } });
    fireEvent.change(screen.getByLabelText('Answer text'), { target: { value: 'Corrected' } });
    fireEvent.change(screen.getByLabelText('Answer status'), { target: { value: 'unknown' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /S1/ }));
    fireEvent.change(screen.getByLabelText('Reviewer'), { target: { value: 'Sam' } });
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Fix' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    expect(screen.getByLabelText('Answer text')).toBeDisabled();
    expect(screen.getByLabelText('Answer status')).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: /S1/ })).toBeDisabled();
    rejectReview(new Error('[localApi] 400: invalid_review_brief: invalid'));
    await screen.findByRole('alert');
    expect(screen.getByLabelText('Answer text')).toHaveValue('Corrected');
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    await waitFor(() =>
      expect(api.reviewInvestigation).toHaveBeenCalledWith(
        'i1',
        expect.objectContaining({
          corrected_brief: expect.objectContaining({
            questions: item.brief.questions,
            answers: [{ question_id: 'Q1', status: 'unknown', text: 'Corrected', snippet_ids: [] }],
            model_uncertainties: [],
          }),
        }),
      ),
    );
  });
  it('shows the registration instructions when a workspace has no collections', async () => {
    api.sourceCollections.mockResolvedValue({ collections: [] });
    render(<InvestigationsView />);
    fireEvent.change(screen.getByLabelText('Workspace'), { target: { value: 'empty' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load collections' }));
    expect(await screen.findByText(/Register and scan a folder/)).toBeInTheDocument();
    expect(api.createInvestigation).not.toHaveBeenCalled();
  });

  it('locks the scope while loading and reports failures without success', async () => {
    let reject!: (error: Error) => void;
    api.sourceCollections.mockReturnValue(
      new Promise((_, fail) => {
        reject = fail;
      }),
    );
    render(<InvestigationsView />);
    fireEvent.change(screen.getByLabelText('Workspace'), { target: { value: 'engineering' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load collections' }));
    expect(screen.getByLabelText('Workspace')).toBeDisabled();
    reject(new Error('Unavailable'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Unavailable');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Workspace')).toBeEnabled();
  });

  it('saves an edited correction and requires an explicit outcome', async () => {
    api.sourceCollections.mockResolvedValue({
      collections: [{ id: 'c1', name: 'Runbooks', scan_status: 'ready', last_scan_at: null }],
    });
    api.investigations.mockResolvedValue({ investigations: [] });
    const brief = {
      facts: [
        {
          text: 'Original',
          citation_ids: ['ref1'],
          source_excerpts: [{ citation_id: 'raw1', quote: 'Evidence' }],
        },
      ],
      prior_fixes: [],
      hypotheses: [],
      recommended_checks: [],
      model_uncertainties: ['Check cause'],
    };
    const item = {
      id: 'i1',
      issue: 'Failure',
      status: 'draft',
      model_slug: 'local-model',
      created_at: 'today',
      evidence: [
        {
          citation_id: 'ref1',
          kind: 'reviewed_claim',
          content: 'Claim',
          sources: [{ citation_id: 'raw1', content: 'Evidence', relative_path: 'incident.md' }],
        },
      ],
      retrieval_notices: ['Some sources unavailable'],
      review: null,
      outcome: null,
      brief,
    };
    api.createInvestigation.mockResolvedValue(item);
    api.reviewInvestigation.mockImplementation(async (_id, request) => ({
      ...item,
      status: 'reviewed',
      review: {
        decision: 'correct',
        reviewer: 'Sam',
        reason: 'Fix',
        corrected_brief: request.corrected_brief,
      },
    }));
    api.reviewInvestigation.mockRejectedValueOnce(
      new Error('[localApi] 400: invalid_review_brief: Check the excerpt.'),
    );
    render(<InvestigationsView />);
    fireEvent.change(screen.getByLabelText('Workspace'), { target: { value: 'w' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load collections' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: /Runbooks/ }));
    fireEvent.change(screen.getByLabelText('Issue'), { target: { value: 'Failure' } });
    fireEvent.change(screen.getByLabelText('Evidence query'), { target: { value: 'failure' } });
    fireEvent.change(screen.getByLabelText('Loaded local model'), {
      target: { value: 'local-model' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create investigation' }));
    await screen.findByRole('heading', { name: 'Failure' });
    expect(screen.getByText('“Evidence”')).toBeInTheDocument();
    expect(screen.getByText('Some sources unavailable')).toBeInTheDocument();
    expect(screen.getByText(/do not establish that a claim is supported/)).toBeInTheDocument();
    const sourceLink = screen.getByRole('link', { name: 'Source 1.1' });
    expect(sourceLink).toHaveAttribute('href', '#investigation-source-1-1');
    fireEvent.click(sourceLink);
    expect(screen.getByText(/raw1.*incident.md/)).toBeVisible();
    await waitFor(() =>
      expect(document.activeElement).toHaveAttribute('id', 'investigation-source-1-1'),
    );
    expect(screen.getByText('Check cause')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Decision'), { target: { value: 'correct' } });
    expect(screen.getByRole('checkbox', { name: /Reviewed claim/ })).toBeChecked();
    fireEvent.change(screen.getByLabelText('Item 1'), { target: { value: 'Corrected' } });
    fireEvent.change(screen.getByLabelText('Reviewer'), { target: { value: 'Sam' } });
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Fix' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Check the correction and exact source excerpts',
    );
    expect(screen.getByLabelText('Item 1')).toHaveValue('Corrected');
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    await waitFor(() =>
      expect(api.reviewInvestigation).toHaveBeenCalledWith(
        'i1',
        expect.objectContaining({
          corrected_brief: expect.objectContaining({
            facts: [
              {
                text: 'Corrected',
                citation_ids: ['ref1'],
                source_excerpts: [{ citation_id: 'raw1', quote: 'Evidence' }],
              },
            ],
            model_uncertainties: ['Check cause'],
          }),
        }),
      ),
    );
    expect(await screen.findByText('Corrected')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Outcome notes'), { target: { value: 'Done' } });
    expect(screen.getByRole('button', { name: 'Save outcome' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Result'), { target: { value: 'resolved' } });
    expect(screen.getByRole('button', { name: 'Save outcome' })).toBeEnabled();
  });

  it.each([
    '[localApi] 404: Investigation unavailable',
    '[localApi] 403: Forbidden',
    '[localApi] 401: Unauthorized',
  ])('clears a correction on unavailable or unauthorized response %s', async (message) => {
    api.sourceCollections.mockResolvedValue({
      collections: [{ id: 'c1', name: 'Runbooks', scan_status: 'ready', last_scan_at: null }],
    });
    api.investigations.mockResolvedValue({ investigations: [] });
    api.createInvestigation.mockResolvedValue({
      id: 'i1',
      issue: 'Failure',
      status: 'draft',
      model_slug: 'local-model',
      created_at: 'today',
      evidence: [],
      review: null,
      outcome: null,
      brief: {
        facts: [],
        prior_fixes: [],
        hypotheses: [],
        recommended_checks: [],
        model_uncertainties: [],
      },
    });
    api.reviewInvestigation.mockRejectedValueOnce(new Error(message));
    render(<InvestigationsView />);
    fireEvent.change(screen.getByLabelText('Workspace'), { target: { value: 'w' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load collections' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: /Runbooks/ }));
    fireEvent.change(screen.getByLabelText('Issue'), { target: { value: 'Failure' } });
    fireEvent.change(screen.getByLabelText('Evidence query'), { target: { value: 'failure' } });
    fireEvent.change(screen.getByLabelText('Loaded local model'), {
      target: { value: 'local-model' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create investigation' }));
    await screen.findByRole('heading', { name: 'Failure' });
    fireEvent.change(screen.getByLabelText('Decision'), { target: { value: 'correct' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add facts item' }));
    fireEvent.change(screen.getByLabelText('Item 1'), { target: { value: 'Private edit' } });
    fireEvent.change(screen.getByLabelText('Reviewer'), { target: { value: 'Sam' } });
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Fix' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(screen.queryByLabelText('Item 1')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Failure' })).not.toBeInTheDocument();
  });

  it('requires an explicit collection and local model before creating a scoped brief', async () => {
    api.sourceCollections.mockResolvedValue({
      collections: [{ id: 'c1', name: 'Runbooks', scan_status: 'ready', last_scan_at: null }],
    });
    api.investigations.mockResolvedValue({ investigations: [] });
    api.createInvestigation.mockResolvedValue({
      id: 'i1',
      issue: 'Deploy failed',
      status: 'draft',
      model_slug: 'local-model',
      created_at: 'today',
      evidence: [],
      review: null,
      outcome: null,
      brief: {
        facts: [],
        prior_fixes: [],
        hypotheses: [],
        recommended_checks: [],
        missing_evidence: [],
      },
    });
    render(<InvestigationsView />);
    fireEvent.change(screen.getByLabelText('Workspace'), { target: { value: 'engineering' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load collections' }));
    await screen.findByText('Runbooks');
    fireEvent.change(screen.getByLabelText('Issue'), { target: { value: 'Deploy failed' } });
    fireEvent.change(screen.getByLabelText('Evidence query'), { target: { value: 'deploy' } });
    fireEvent.change(screen.getByLabelText('Loaded local model'), {
      target: { value: 'local-model' },
    });
    expect(screen.getByRole('button', { name: 'Create investigation' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /Runbooks/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Answer each question/ }));
    fireEvent.change(screen.getByLabelText('Answer profile'), {
      target: { value: 'qwen3-non-thinking' },
    });
    fireEvent.click(screen.getByRole('checkbox', { name: /Answer each question/ }));
    expect(screen.queryByLabelText('Answer profile')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Create investigation' }));
    await waitFor(() =>
      expect(api.createInvestigation).toHaveBeenCalledWith({
        workspace: 'engineering',
        collection_ids: ['c1'],
        issue: 'Deploy failed',
        query: 'deploy',
        model_slug: 'local-model',
      }),
    );
    expect(await screen.findByRole('heading', { name: 'Deploy failed' })).toBeInTheDocument();
    expect(
      screen.getByRole('heading', {
        name: 'Historical combined missing evidence (provenance unknown)',
      }),
    ).toBeInTheDocument();
  });
});
