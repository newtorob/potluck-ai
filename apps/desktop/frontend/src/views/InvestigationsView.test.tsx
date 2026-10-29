import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { InvestigationsView } from './InvestigationsView';

const api = vi.hoisted(() => ({
  sourceCollections: vi.fn(),
  investigations: vi.fn(),
  createInvestigation: vi.fn(),
  reviewInvestigation: vi.fn(),
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
