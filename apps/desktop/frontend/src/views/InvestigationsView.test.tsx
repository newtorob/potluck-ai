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

  it('clears a correction when the investigation becomes unavailable', async () => {
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
    api.reviewInvestigation.mockRejectedValueOnce(
      new Error('[localApi] 404: Investigation unavailable'),
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
    fireEvent.change(screen.getByLabelText('Decision'), { target: { value: 'correct' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add facts item' }));
    fireEvent.change(screen.getByLabelText('Item 1'), { target: { value: 'Private edit' } });
    fireEvent.change(screen.getByLabelText('Reviewer'), { target: { value: 'Sam' } });
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Fix' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Investigation unavailable');
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
