import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { GuidedCompanyBrainView } from './GuidedCompanyBrainView';
import { useCompanyBrainWorkspaceStore } from '@/stores/companyBrainWorkspaceStore';
import type { PilotClaim } from '@/types/companyBrainPilot';
const api = vi.hoisted(() => ({
  companyBrainModelSetup: vi.fn().mockResolvedValue(null),
  importCompanyBrainModel: vi.fn(),
  prepareCompanyBrainModel: vi.fn(),
  unloadCompanyBrainModel: vi.fn(),
  sourceCollections: vi.fn(),
  selectDirectory: vi.fn(),
  investigations: vi.fn(),
  investigation: vi.fn(),
  pilotSetupDrafts: vi.fn(),
  pilotSourceDraft: vi.fn(),
  pilotClaims: vi.fn(),
  proposePilotClaim: vi.fn(),
  approvePilotClaim: vi.fn(),
  supersedePilotClaim: vi.fn(),
  askReviewedPilot: vi.fn(),
  reviewInvestigation: vi.fn(),
  exportInvestigation: vi.fn(),
  scanPilotCollection: vi.fn(),
  registerPilotCollection: vi.fn(),
  fetchModels: vi.fn().mockResolvedValue(undefined),
  health: vi.fn(),
}));
vi.mock('@/lib/localApi', () => ({ localApi: api }));
vi.mock('@/lib/ipc', () => ({ ipc: { selectDirectory: api.selectDirectory } }));
vi.mock('@/lib/sidecarPersist', () => ({ persistUiPref: vi.fn().mockResolvedValue(undefined) }));
vi.mock('@/stores/modelStore', () => ({
  useModelStore: (selector: (s: unknown) => unknown) =>
    selector({ activeModelSlug: 'loaded-model', fetchModels: api.fetchModels }),
}));
const source = {
  citation_id: 'source:chunk:rev1',
  collection_id: 'c1',
  document_id: 'd1',
  revision_id: 'rev1',
  chunk_id: 'chunk',
  relative_path: 'policy.md',
  heading: 'Draft',
  content: 'Approval required unless incident response applies.',
};
const preview = {
  preview_id: 'pv1',
  content: 'Draft policy. Approval required unless incident response applies.',
  citation_ids: [source.citation_id],
  documents: [
    {
      relative_path: 'policy.md',
      collection_name: 'Docs',
      collection_id: 'c1',
      document_id: 'd1',
      revision_id: 'rev1',
      opening: [source],
      excerpts: [source],
    },
  ],
  limitations: ['Review the full source.'],
};
const draft = {
  relative_path: 'policy.md',
  collection_id: 'c1',
  document_id: 'd1',
  revision_id: 'rev1',
  preview,
  notice: '',
};
const report = {
  collection_id: 'c1',
  files_scanned: 1,
  files_added: 0,
  files_updated: 1,
  files_removed: 0,
  files_unchanged: 0,
  chunks_inserted: 1,
  invalidated_answers: 1,
  revoked_claims: 1,
  scanned_at: new Date().toISOString(),
  changes: [{ relative_path: 'policy.md', change: 'updated' as const }],
  changes_truncated: false,
};
const collection = {
  id: 'c1',
  name: 'Docs',
  scan_status: 'ready',
  last_scan_at: report.scanned_at,
  last_scan_report: report,
};
const claim: PilotClaim = {
  id: 'claim1',
  claim_key: 'document:d1',
  content: preview.content,
  status: 'approved',
  sources: [source],
};
const result = {
  id: 'answer1',
  workspace: 'w',
  collection_ids: ['c1'],
  issue: 'Who approves?',
  model_slug: 'loaded-model',
  status: 'draft',
  created_at: 'today',
  review: null,
  outcome: null,
  evidence: [],
  brief: {
    facts: [],
    prior_fixes: [],
    hypotheses: [],
    recommended_checks: [],
    answers: [
      {
        question_id: 'R1',
        status: 'unknown',
        text: 'Not established by the selected statements.',
        snippet_ids: [],
      },
    ],
  },
};
let approved: PilotClaim[] = [];
let proposed: PilotClaim[] = [];
function mount(
  reviewedEvidenceVersion: 'v5' | 'v6' = 'v5',
  reviewedAnswerVersion: 'v32' | 'v33' | 'v34' | 'v35' | 'v36' = 'v32',
) {
  return render(
    <MemoryRouter>
      <GuidedCompanyBrainView
        onAdvanced={vi.fn()}
        reviewedEvidenceVersion={reviewedEvidenceVersion}
        reviewedAnswerVersion={reviewedAnswerVersion}
      />
    </MemoryRouter>,
  );
}
async function question() {
  await screen.findByRole('checkbox', { name: /Docs/ });
  fireEvent.change(screen.getByLabelText('Your question'), { target: { value: 'Who approves?' } });
  fireEvent.click(screen.getByRole('button', { name: 'Review sources for this question' }));
  await screen.findByRole('article', { name: 'Source policy.md' });
}
async function confirm() {
  fireEvent.click(screen.getByRole('button', { name: /Continue with current reviewed/ }));
  fireEvent.click(screen.getByRole('checkbox', { name: 'I reviewed these question parts.' }));
}
beforeEach(() => {
  vi.resetAllMocks();
  api.companyBrainModelSetup.mockResolvedValue(pilotSetup());
  approved = [];
  proposed = [];
  useCompanyBrainWorkspaceStore.setState({
    saved: { version: 1, workspace: 'w', collectionIds: ['c1'], modelSlug: 'loaded-model' },
    revision: 0,
  });
  api.sourceCollections.mockResolvedValue({ collections: [collection] });
  api.investigations.mockResolvedValue({ investigations: [] });
  api.fetchModels.mockResolvedValue(undefined);
  api.health.mockResolvedValue({ model_loaded: true, active_model_id: 'loaded-model' });
  api.pilotSetupDrafts.mockResolvedValue({
    version: 'pilot-setup-drafts-v1',
    question: 'Who approves?',
    question_parts: { parts: ['Who approves?'], coverage: 'not_verified', limitations: [] },
    query_trace: { effective_query: 'approval', omitted_terms: [] },
    candidates: [draft],
    truncated: false,
    limitations: [],
  });
  api.pilotSourceDraft.mockResolvedValue(draft);
  api.pilotClaims.mockImplementation(async (input: { status: string }) => ({
    claims: input.status === 'approved' ? approved : proposed,
    truncated: false,
  }));
  api.proposePilotClaim.mockImplementation(async () => {
    proposed = [{ ...claim, status: 'proposed' }];
    return { claim_id: 'claim1' };
  });
  api.approvePilotClaim.mockImplementation(async () => {
    approved = [claim];
    proposed = [];
    return { ok: true };
  });
  api.askReviewedPilot.mockResolvedValue(result);
  api.investigation.mockResolvedValue(result);
  api.scanPilotCollection.mockResolvedValue(report);
});
function pilotSetup(overrides: Record<string, unknown> = {}) {
  return {
    version: 'company-brain-model-setup-v1',
    model: {
      id: 'pilot',
      name: 'Pilot',
      sha256: 'abc',
      size_bytes: 100,
      context_tokens: 8192,
      backend: 'llama-server',
    },
    platform_supported: true,
    installed: true,
    artifact_verified: true,
    ready: false,
    can_import: false,
    can_prepare: true,
    can_unload: false,
    active_model: 'loaded-model',
    issues: [],
    resources: {
      estimated_ram_bytes: 100,
      available_ram_bytes: 200,
      required_disk_bytes: 0,
      disk_free_bytes: 200,
    },
    warnings: [],
    ...overrides,
  };
}

describe('guided pilot setup integration', () => {
  it('checks setup passively without importing or preparing', async () => {
    api.companyBrainModelSetup.mockResolvedValue(pilotSetup());
    mount();
    await screen.findByText('Model imported; not ready');
    expect(api.companyBrainModelSetup).toHaveBeenCalledTimes(1);
    expect(api.importCompanyBrainModel).not.toHaveBeenCalled();
    expect(api.prepareCompanyBrainModel).not.toHaveBeenCalled();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
  });
  it('blocks source and question actions during prepare and selects only a ready result', async () => {
    api.companyBrainModelSetup.mockResolvedValue(pilotSetup());
    let resolve!: (value: unknown) => void;
    api.prepareCompanyBrainModel.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    mount();
    await screen.findByText('Model imported; not ready');
    fireEvent.change(screen.getByLabelText('Your question'), {
      target: { value: 'Who approves?' },
    });
    fireEvent.click(screen.getByRole('checkbox', { name: /prepare this model/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Prepare model' }));
    expect(screen.getByLabelText('Your question')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Review sources for this question' })).toBeDisabled();
    expect(api.pilotSetupDrafts).not.toHaveBeenCalled();
    expect(api.prepareCompanyBrainModel).toHaveBeenCalledWith({
      prepare_confirmed: true,
      expected_active_model: 'loaded-model',
    });
    resolve(
      pilotSetup({ ready: true, active_model: 'pilot', can_prepare: false, can_unload: true }),
    );
    await screen.findByText('Ready for local questions');
    expect(useCompanyBrainWorkspaceStore.getState().saved?.modelSlug).toBe('pilot');
    expect(api.fetchModels).toHaveBeenCalled();
  });
  it('keeps a saved answer and review available after unload and requires reconfirmation', async () => {
    api.companyBrainModelSetup.mockResolvedValue(
      pilotSetup({ ready: true, active_model: 'pilot', can_prepare: false, can_unload: true }),
    );
    api.unloadCompanyBrainModel.mockResolvedValue(pilotSetup({ active_model: null }));
    await openReviewAnswer();
    expect(screen.getByRole('region', { name: 'Review and handoff' })).toBeVisible();
    const reconfirm = screen.getByRole('checkbox', { name: 'I reviewed these question parts.' });
    if (!(reconfirm as HTMLInputElement).checked) fireEvent.click(reconfirm);
    expect(screen.getByRole('button', { name: 'Get answer' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'End model session' }));
    await waitFor(() =>
      expect(api.unloadCompanyBrainModel).toHaveBeenCalledWith({ expected_active_model: 'pilot' }),
    );
    await waitFor(() => expect(screen.getByRole('button', { name: 'Get answer' })).toBeDisabled());
    expect(screen.getByRole('region', { name: 'Your answer' })).toBeVisible();
    expect(screen.getByRole('region', { name: 'Review and handoff' })).toBeVisible();
  });
});

describe('guided Company Brain', () => {
  it('proposes a compound question without manual part edits and sends the unchanged original only after review', async () => {
    approved = [claim];
    mount();
    await screen.findByRole('checkbox', { name: /Docs/ });
    const original = 'What caused the import failure, and what recovery was implemented?';
    fireEvent.change(screen.getByLabelText('Your question'), { target: { value: original } });
    fireEvent.click(screen.getByRole('button', { name: 'Review sources for this question' }));
    fireEvent.click(await screen.findByRole('button', { name: /Continue with current/ }));
    expect(screen.queryByLabelText('Part 1')).not.toBeInTheDocument();
    expect(screen.getByText('and what recovery was implemented?')).toBeVisible();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
    expect(api.approvePilotClaim).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'I reviewed these question parts.' }));
    fireEvent.click(screen.getByRole('button', { name: 'Get answer' }));
    await screen.findByRole('region', { name: 'Your answer' });
    expect(api.askReviewedPilot).toHaveBeenCalledWith(
      expect.objectContaining({
        question: original,
        question_parts: ['What caused the import failure,', ' and what recovery was implemented?'],
        question_parts_confirmed: true,
      }),
    );
  });
  it('requires confirmation again after keeping a compound question whole or restoring suggestions', async () => {
    approved = [claim];
    mount();
    await screen.findByRole('checkbox', { name: /Docs/ });
    fireEvent.change(screen.getByLabelText('Your question'), {
      target: { value: 'What changed, and what remains?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Review sources for this question' }));
    await screen.findByRole('button', { name: /Continue with current/ });
    await confirm();
    fireEvent.click(screen.getByRole('button', { name: 'Keep as one question' }));
    expect(screen.getByRole('button', { name: 'Get answer' })).toBeDisabled();
    expect(
      within(screen.getByRole('region', { name: 'Confirm question' })).getByText(
        'What changed, and what remains?',
      ),
    ).toBeVisible();
    fireEvent.click(screen.getByRole('checkbox', { name: 'I reviewed these question parts.' }));
    fireEvent.click(screen.getByRole('button', { name: 'Restore suggestions' }));
    expect(screen.getByRole('button', { name: 'Get answer' })).toBeDisabled();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
  });
  it('preserves edited parts across a source setup failure and retry but clears confirmation', async () => {
    approved = [claim];
    mount();
    await question();
    await confirm();
    fireEvent.click(screen.getByRole('button', { name: 'Edit question parts' }));
    fireEvent.change(screen.getByLabelText('Part 1'), {
      target: { value: 'Who approves an exception?' },
    });
    api.pilotSetupDrafts.mockRejectedValueOnce(new Error('source unavailable'));
    fireEvent.click(screen.getByRole('button', { name: 'Review sources for this question' }));
    await screen.findByRole('alert');
    expect(screen.getByLabelText('Your question')).toHaveValue('Who approves?');
    fireEvent.click(screen.getByRole('button', { name: 'Review sources for this question' }));
    fireEvent.click(await screen.findByRole('button', { name: /Continue with current/ }));
    expect(screen.getByText('Who approves an exception?')).toBeVisible();
    expect(
      screen.getByRole('checkbox', { name: 'I reviewed these question parts.' }),
    ).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Get answer' })).toBeDisabled();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
  });
  it('keeps edited parts after a failed scan while requiring new source preparation and confirmation', async () => {
    approved = [claim];
    mount();
    await question();
    await confirm();
    fireEvent.click(screen.getByRole('button', { name: 'Edit question parts' }));
    fireEvent.change(screen.getByLabelText('Part 1'), {
      target: { value: 'Who approves the documented exception?' },
    });
    fireEvent.click(screen.getByText(/Workspace and folders/));
    api.scanPilotCollection.mockRejectedValueOnce(new Error('scan failed'));
    fireEvent.click(screen.getByRole('button', { name: 'Scan Docs' }));
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: 'Review sources for this question' }));
    fireEvent.click(await screen.findByRole('button', { name: /Continue with current/ }));
    expect(screen.getByText('Who approves the documented exception?')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Get answer' })).toBeDisabled();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
  });
  it.each(['v33', 'v34', 'v35', 'v36'] as const)(
    'sends explicit answer version %s',
    async (version) => {
      approved = [claim];
      mount('v5', version);
      await question();
      await confirm();
      fireEvent.click(screen.getByRole('button', { name: 'Get answer' }));
      await screen.findByRole('region', { name: 'Your answer' });
      expect(api.askReviewedPilot).toHaveBeenCalledWith(
        expect.objectContaining({ reviewed_answer_version: version }),
      );
    },
  );
  it('opts into whole-statement packing only when explicitly selected', async () => {
    approved = [claim];
    mount('v6');
    await question();
    await confirm();
    fireEvent.click(screen.getByRole('button', { name: 'Get answer' }));
    await screen.findByRole('region', { name: 'Your answer' });
    expect(api.askReviewedPilot).toHaveBeenCalledWith(
      expect.objectContaining({
        reviewed_evidence_version: 'v6',
        question_parts_confirmed: true,
      }),
    );
  });
  it('shows omitted matching statements above a reopened answer without rerunning it', async () => {
    const savedAnswer = {
      ...result,
      provenance: {
        reviewed_selection: {
          version: 'whole-claim-snippet-budget-v1',
          selected_claims: 2,
          omitted_claims: 1,
        },
      },
    };
    api.investigations.mockResolvedValue({ investigations: [savedAnswer] });
    api.investigation.mockResolvedValue(savedAnswer);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Who approves? · draft' }));
    await screen.findByRole('region', { name: 'Your answer' });
    expect(screen.getByText(/2 reviewed statements were supplied/)).toHaveTextContent(
      '1 other matching statement was left out to stay within the evidence limit',
    );
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
    expect(api.approvePilotClaim).not.toHaveBeenCalled();
  });
  it('keeps literal evidence and citations intact and exposes structured values on reopen without inference', async () => {
    const text = 'Approval required unless incident response applies.';
    const savedAnswer = {
      ...result,
      evidence: [
        {
          citation_id: 'source:exact:rev1',
          content: text,
          sources: [
            {
              relative_path: 'policy.md',
              heading: 'Conditions',
              content: { prerequisite: 'owner approval', exception: 'incident response' },
            },
          ],
        },
      ],
    };
    api.investigations.mockResolvedValue({ investigations: [savedAnswer] });
    api.investigation.mockResolvedValue(savedAnswer);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Who approves? · draft' }));
    await screen.findByRole('region', { name: 'Your answer' });
    fireEvent.click(screen.getByText('policy.md'));
    expect(screen.getByText('source:exact:rev1')).toBeVisible();
    expect(screen.getByText(text)).toBeVisible();
    expect(
      screen.getByText('{"prerequisite":"owner approval","exception":"incident response"}'),
    ).toBeVisible();
    expect(screen.queryByText('[object Object]')).not.toBeInTheDocument();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
    expect(api.approvePilotClaim).not.toHaveBeenCalled();
  });
  it('distinguishes initial connection checking and gives a read-only retry after failure', async () => {
    let reject!: (error: Error) => void;
    api.health.mockReturnValueOnce(
      new Promise((_, fail) => {
        reject = fail;
      }),
    );
    mount();
    expect(screen.getByRole('heading', { name: 'Checking local engine…' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check connection' })).toBeDisabled();
    reject(new Error('offline'));
    expect(
      await screen.findByRole('heading', { name: 'Local engine unavailable' }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Check connection' }));
    expect(
      await screen.findByRole('heading', { name: 'Ready to review sources' }),
    ).toBeInTheDocument();
    expect(api.health).toHaveBeenCalledTimes(2);
    expect(api.scanPilotCollection).not.toHaveBeenCalled();
    expect(api.proposePilotClaim).not.toHaveBeenCalled();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
  });
  it('keeps source review available without a model but blocks getting an answer', async () => {
    api.health.mockResolvedValue({ model_loaded: false, active_model_id: null });
    mount();
    await question();
    expect(screen.getByText(/No model loaded. Review sources now/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Approve document for answers' }));
    await screen.findByRole('button', { name: 'Already reviewed' });
    await confirm();
    expect(screen.getByRole('button', { name: 'Get answer' })).toBeDisabled();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
  });
  it('explains failed startup restore without loading or switching a model', async () => {
    api.health.mockResolvedValue({
      model_loaded: false,
      active_model_id: null,
      model_restore: { status: 'failed', model_id: 'saved-model' },
    });
    mount();
    expect(await screen.findByText(/Startup restore failed/)).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'Open Models' })[0]).toHaveAttribute(
      'href',
      '/models',
    );
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
  });
  it('invalidates question confirmation after connection loss and does not revive it on recovery', async () => {
    approved = [claim];
    mount();
    await question();
    await confirm();
    expect(screen.getByRole('button', { name: 'Get answer' })).toBeEnabled();
    api.health.mockRejectedValueOnce(new Error('offline'));
    fireEvent(document, new Event('visibilitychange'));
    await screen.findByRole('heading', { name: 'Local engine unavailable' });
    expect(screen.getByRole('button', { name: 'Get answer' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Check connection' }));
    await screen.findByRole('heading', { name: 'Ready to review sources' });
    expect(
      screen.getByRole('checkbox', { name: 'I reviewed these question parts.' }),
    ).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Get answer' })).toBeDisabled();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
  });
  it('blocks stale scans and explains the next action in the visible readiness summary', async () => {
    api.sourceCollections.mockResolvedValue({
      collections: [{ ...collection, last_scan_at: '2000-01-01T00:00:00Z' }],
    });
    mount();
    await screen.findByText(/Scan every selected folder successfully. Scans must/);
    fireEvent.change(screen.getByLabelText('Your question'), { target: { value: 'Question?' } });
    expect(screen.getByRole('button', { name: 'Review sources for this question' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Set up workspace and folders' }));
    expect(screen.getByRole('textbox', { name: 'Workspace name' })).toBeVisible();
    expect(api.pilotSetupDrafts).not.toHaveBeenCalled();
  });
  it('collapses paths while retaining failed-scan context, counts and truncation notice', async () => {
    api.sourceCollections.mockResolvedValue({
      collections: [
        {
          ...collection,
          scan_status: 'failed',
          last_scan_report: {
            ...report,
            files_removed: 1,
            changes_truncated: true,
            changes: [...report.changes, { relative_path: 'removed.md', change: 'removed' }],
          },
        },
      ],
    });
    mount();
    await screen.findByText(/The report below is from its last successful scan/);
    expect(
      screen.getByText(/Approved statements revoked: 1. Saved answers cleared: 1/),
    ).toBeVisible();
    expect(screen.getByText(/Showing the first 100 changed paths/)).toBeVisible();
    expect(screen.getByText('Changed documents (2)').closest('details')).not.toHaveAttribute(
      'open',
    );
    fireEvent.click(screen.getByText('Changed documents (2)'));
    expect(screen.getByRole('button', { name: 'Review policy.md' })).toBeDisabled();
    expect(screen.getByText('removed: removed.md')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Review removed.md' })).not.toBeInTheDocument();
  });
  it('shows an explicit unchanged scan state without an empty path disclosure', async () => {
    api.sourceCollections.mockResolvedValue({
      collections: [
        {
          ...collection,
          last_scan_report: {
            ...report,
            files_updated: 0,
            revoked_claims: 0,
            invalidated_answers: 0,
            changes: [],
          },
        },
      ],
    });
    mount();
    expect(
      await screen.findByText('No document changes in the last successful scan.'),
    ).toBeVisible();
    expect(screen.queryByText(/Changed documents \(/)).not.toBeInTheDocument();
  });
  it('makes omitted query words visible even when document search is otherwise complete', async () => {
    api.pilotSetupDrafts.mockResolvedValueOnce({
      version: 'pilot-setup-drafts-v2',
      question: 'Who approves?',
      query_trace: { effective_query: 'approval', omitted_terms: ['billing'] },
      question_parts: { parts: ['Who approves?'], coverage: 'not_verified', limitations: [] },
      candidates: [draft],
      truncated: false,
      limitations: [],
    });
    mount();
    await question();
    expect(await screen.findByText(/More matching documents or indexed text/)).toBeInTheDocument();
    fireEvent.click(screen.getByText('How documents were suggested'));
    expect(
      screen.getByText(/Some question words were not used for ranking: billing/),
    ).toBeInTheDocument();
    expect(api.approvePilotClaim).not.toHaveBeenCalled();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
  });
  it('shows bounded word-match explanations without authorizing an answer and clears them on question changes', async () => {
    api.pilotSetupDrafts.mockResolvedValueOnce({
      version: 'pilot-setup-drafts-v2',
      question: 'Who approves?',
      question_parts: { parts: ['Who approves?'], coverage: 'not_verified', limitations: [] },
      candidates: [
        {
          ...draft,
          match: {
            version: 'document-topics-v4',
            title_terms: ['approval'],
            text_terms: ['owner'],
          },
        },
      ],
      truncated: true,
      limitations: ['Word matches are not approval.'],
    });
    mount();
    await question();
    expect(await screen.findByText(/Normalized word matches/)).toHaveTextContent('approval');
    expect(screen.getByText(/More matching documents or indexed text/)).toBeInTheDocument();
    expect(api.approvePilotClaim).not.toHaveBeenCalled();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole('textbox', { name: 'Your question' }), {
      target: { value: 'Other question?' },
    });
    expect(screen.queryByText(/Normalized word matches/)).not.toBeInTheDocument();
    expect(screen.queryByText(/More matching documents or indexed text/)).not.toBeInTheDocument();
    expect(screen.queryByText('How documents were suggested')).not.toBeInTheDocument();
  });
  it('requires explicit source approval and question confirmation, then makes exactly one answer request', async () => {
    mount();
    await question();
    expect(api.pilotSetupDrafts).toHaveBeenCalledWith({
      workspace: 'w',
      collection_ids: ['c1'],
      question: 'Who approves?',
      suggestion_version: 'document-topics-v4',
    });
    expect(api.proposePilotClaim).not.toHaveBeenCalled();
    expect(api.approvePilotClaim).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Approve document for answers' }));
    await screen.findByText('Source review saved. Confirm what the question should cover.');
    expect(api.proposePilotClaim).toHaveBeenCalledWith({
      workspace: 'w',
      collection_ids: ['c1'],
      claim_key: 'document:d1',
      content: preview.content,
      citation_ids: preview.citation_ids,
    });
    const get = screen.getByRole('button', { name: 'Get answer' });
    expect(get).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'I reviewed these question parts.' }));
    fireEvent.click(get);
    fireEvent.click(get);
    await screen.findByRole('region', { name: 'Your answer' });
    expect(api.askReviewedPilot).toHaveBeenCalledTimes(1);
    expect(api.askReviewedPilot).toHaveBeenCalledWith({
      workspace: 'w',
      collection_ids: ['c1'],
      question: 'Who approves?',
      model_slug: 'loaded-model',
      pilot_confirmed: true,
      question_parts: ['Who approves?'],
      question_parts_confirmed: true,
    });
  });
  it('reuses an existing exact approval and clears confirmation after a question-part edit', async () => {
    approved = [{ ...claim, claim_key: 'legacy-key' }];
    mount();
    await question();
    expect(screen.getByRole('button', { name: 'Already reviewed' })).toBeDisabled();
    await confirm();
    expect(screen.getByRole('button', { name: 'Get answer' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Edit question parts' }));
    fireEvent.change(screen.getByLabelText('Part 1'), {
      target: { value: 'Who approves an incident?' },
    });
    expect(screen.getByRole('button', { name: 'Get answer' })).toBeDisabled();
    expect(api.proposePilotClaim).not.toHaveBeenCalled();
    expect(api.approvePilotClaim).not.toHaveBeenCalled();
  });
  it('opens a changed document without replacing or inventing the question', async () => {
    mount();
    await screen.findByRole('checkbox', { name: /Docs/ });
    fireEvent.change(screen.getByLabelText('Your question'), {
      target: { value: 'Keep this question' },
    });
    fireEvent.click(screen.getByText('Changed documents (1)'));
    fireEvent.click(screen.getByRole('button', { name: 'Review policy.md' }));
    await screen.findByRole('article', { name: 'Source policy.md' });
    expect(api.pilotSourceDraft).toHaveBeenCalledWith({
      workspace: 'w',
      collection_ids: ['c1'],
      collection_id: 'c1',
      relative_path: 'policy.md',
    });
    expect(screen.getByLabelText('Your question')).toHaveValue('Keep this question');
    expect(api.pilotSetupDrafts).not.toHaveBeenCalled();
    expect(api.approvePilotClaim).not.toHaveBeenCalled();
  });
  it('reuses a matching proposal after uncertain approval and never automatically retries a write', async () => {
    proposed = [{ ...claim, status: 'proposed' }];
    api.approvePilotClaim.mockRejectedValueOnce(new Error('timeout'));
    mount();
    await question();
    fireEvent.click(screen.getByRole('button', { name: 'Approve document for answers' }));
    await screen.findByRole('alert');
    expect(api.approvePilotClaim).toHaveBeenCalledTimes(1);
    expect(api.proposePilotClaim).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Review sources for this question' }));
    await screen.findByRole('article', { name: 'Source policy.md' });
    fireEvent.click(screen.getByRole('button', { name: 'Approve document for answers' }));
    await screen.findByText('Source review saved. Confirm what the question should cover.');
    expect(api.approvePilotClaim).toHaveBeenCalledTimes(2);
    expect(api.proposePilotClaim).not.toHaveBeenCalled();
  });
  it('explicitly supersedes a different single-document approval under its existing key', async () => {
    approved = [
      { ...claim, id: 'old', claim_key: 'original-key', content: 'Earlier reviewed statement.' },
    ];
    api.supersedePilotClaim.mockImplementation(async () => {
      approved = [{ ...claim, claim_key: 'original-key' }];
      return { ok: true };
    });
    mount();
    await question();
    fireEvent.click(screen.getByRole('button', { name: 'Approve and replace previous statement' }));
    await screen.findByText('Source review saved. Confirm what the question should cover.');
    expect(api.proposePilotClaim).toHaveBeenCalledWith(
      expect.objectContaining({ claim_key: 'original-key' }),
    );
    expect(api.supersedePilotClaim).toHaveBeenCalledWith(
      'old',
      expect.objectContaining({ replacement_id: 'claim1' }),
    );
    expect(api.approvePilotClaim).not.toHaveBeenCalled();
  });
  it('does not guess which of multiple current statements to replace', async () => {
    approved = [
      { ...claim, id: 'a', content: 'A' },
      { ...claim, id: 'b', content: 'B', claim_key: 'other' },
    ];
    mount();
    await question();
    expect(screen.getByRole('button', { name: 'Approve document for answers' })).toBeDisabled();
    expect(screen.getByText(/Several reviewed statements reference/)).toBeVisible();
    expect(api.supersedePilotClaim).not.toHaveBeenCalled();
  });
  it('loads exact-scope recent questions without inference and clears a stale saved answer on open', async () => {
    api.investigations.mockResolvedValue({ investigations: [result] });
    api.investigation.mockRejectedValue(new Error('[localApi] 404: unavailable'));
    mount();
    const section = await screen.findByRole('region', { name: 'Recent questions' });
    fireEvent.click(await within(section).findByRole('button', { name: 'Who approves? · draft' }));
    await screen.findByRole('alert');
    expect(screen.queryByRole('region', { name: 'Your answer' })).not.toBeInTheDocument();
    expect(api.askReviewedPilot).not.toHaveBeenCalled();
  });
  it('drops late recent results after a scan, clears question confirmation and refreshes the current list', async () => {
    let finish!: (data: { investigations: (typeof result)[] }) => void;
    api.investigations
      .mockReturnValueOnce(
        new Promise((resolve) => {
          finish = resolve;
        }),
      )
      .mockResolvedValue({ investigations: [] });
    approved = [claim];
    mount();
    await question();
    await confirm();
    fireEvent.click(screen.getByRole('button', { name: 'Scan Docs' }));
    await screen.findByText(/Scan complete/);
    finish({ investigations: [result] });
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: 'Who approves? · draft' }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole('button', { name: 'Get answer' })).not.toBeInTheDocument();
  });
});

it('recovers a committed approval after timeout without another authority write', async () => {
  api.approvePilotClaim.mockImplementationOnce(async () => {
    approved = [claim];
    proposed = [];
    throw new Error('timeout after commit');
  });
  mount();
  await question();
  fireEvent.click(screen.getByRole('button', { name: 'Approve document for answers' }));
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: 'Review sources for this question' }));
  await screen.findByRole('article', { name: 'Source policy.md' });
  expect(screen.getByRole('button', { name: 'Already reviewed' })).toBeDisabled();
  expect(api.proposePilotClaim).toHaveBeenCalledTimes(1);
  expect(api.approvePilotClaim).toHaveBeenCalledTimes(1);
  await confirm();
  expect(screen.getByRole('button', { name: 'Get answer' })).toBeEnabled();
});

it('keeps an incomplete knowledge inventory from authorizing the guided path', async () => {
  api.pilotClaims.mockResolvedValue({ claims: [claim], truncated: true });
  mount();
  await question();
  expect(screen.getByText(/knowledge list is incomplete/)).toBeVisible();
  expect(screen.getByRole('button', { name: /Continue with current reviewed/ })).toBeDisabled();
  expect(api.askReviewedPilot).not.toHaveBeenCalled();
});

it('does not restore a late recent result from a previously selected folder scope', async () => {
  let finish!: (data: { investigations: (typeof result)[] }) => void;
  api.investigations.mockReturnValueOnce(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  mount();
  const checkbox = await screen.findByRole('checkbox', { name: /Docs/ });
  fireEvent.click(checkbox);
  finish({ investigations: [result] });
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: 'Who approves? · draft' })).not.toBeInTheDocument(),
  );
  expect(api.askReviewedPilot).not.toHaveBeenCalled();
});

it('selects a native folder without granting scan consent and resets consent on path edits', async () => {
  api.selectDirectory.mockResolvedValue('/sandbox/docs');
  mount();
  await screen.findByRole('checkbox', { name: /Docs/ });
  fireEvent.click(screen.getByText('Add a document folder'));
  fireEvent.click(screen.getByRole('button', { name: 'Choose folder' }));
  await waitFor(() =>
    expect(screen.getByLabelText('Full folder path')).toHaveValue('/sandbox/docs'),
  );
  const consent = screen.getByRole('checkbox', {
    name: 'Allow Potluck to read this folder for this workspace.',
  });
  expect(consent).not.toBeChecked();
  fireEvent.click(consent);
  fireEvent.change(screen.getByLabelText('Full folder path'), {
    target: { value: '/different/path' },
  });
  expect(consent).not.toBeChecked();
  expect(api.registerPilotCollection).not.toHaveBeenCalled();
  expect(api.scanPilotCollection).not.toHaveBeenCalled();
});

async function openReviewAnswer(value = result) {
  approved = [claim];
  api.askReviewedPilot.mockResolvedValue(value);
  const view = mount();
  await question();
  await confirm();
  fireEvent.click(screen.getByRole('button', { name: 'Get answer' }));
  await screen.findByRole('region', { name: 'Review and handoff' });
  return view;
}
function fillReview(decision = 'accept') {
  const panel = within(screen.getByRole('region', { name: 'Review and handoff' }));
  fireEvent.change(panel.getByLabelText('Decision'), { target: { value: decision } });
  fireEvent.change(panel.getByLabelText('Reviewer name'), { target: { value: 'Test reviewer' } });
  fireEvent.change(panel.getByLabelText('Reason'), {
    target: { value: 'Checked conditions and source references.' },
  });
  return panel;
}
function reviewed(value = result, decision = 'accept') {
  return {
    ...structuredClone(value),
    status: 'reviewed',
    review: {
      decision,
      reviewer: 'Test reviewer',
      reason: 'Checked conditions and source references.',
    },
  };
}
describe('guided saved-answer review and handoff', () => {
  it('keeps failed review entries and reconciles by GET without repeating the POST', async () => {
    await openReviewAnswer();
    api.reviewInvestigation.mockRejectedValue(new Error('503 connection uncertain'));
    api.investigation.mockResolvedValue(structuredClone(result));
    const panel = fillReview();
    fireEvent.click(panel.getByRole('button', { name: 'Save review' }));
    await screen.findByText(/Could not complete this action. Your entries are kept/);
    expect(api.reviewInvestigation).toHaveBeenCalledTimes(1);
    expect(api.investigation).toHaveBeenLastCalledWith('answer1', {
      workspace: 'w',
      collection_ids: ['c1'],
    });
    expect(panel.getByLabelText('Reviewer name')).toHaveValue('Test reviewer');
    expect(panel.getByLabelText('Reason')).toHaveValue('Checked conditions and source references.');
    expect(screen.getByRole('region', { name: 'Your answer' })).toBeVisible();
  });
  it('confirms an already committed review after a lost response, with no automatic second write', async () => {
    await openReviewAnswer();
    api.reviewInvestigation.mockRejectedValue(new Error('response lost'));
    api.investigation.mockResolvedValue(reviewed());
    const panel = fillReview();
    fireEvent.click(panel.getByRole('button', { name: 'Save review' }));
    await screen.findByText('Recorded decision: accept');
    expect(api.reviewInvestigation).toHaveBeenCalledTimes(1);
    expect(api.investigation).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Save review' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(api.askReviewedPilot).toHaveBeenCalledTimes(1);
  });
  it('blocks duplicate review activation and binds the saved write to the exact scope', async () => {
    await openReviewAnswer();
    let resolve!: (value: unknown) => void;
    api.reviewInvestigation.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const panel = fillReview('reject');
    const save = panel.getByRole('button', { name: 'Save review' });
    fireEvent.click(save);
    fireEvent.click(save);
    expect(api.reviewInvestigation).toHaveBeenCalledTimes(1);
    expect(api.reviewInvestigation).toHaveBeenCalledWith('answer1', {
      workspace: 'w',
      collection_ids: ['c1'],
      decision: 'reject',
      reviewer: 'Test reviewer',
      reason: 'Checked conditions and source references.',
    });
    resolve(reviewed(result, 'reject'));
    await screen.findByText('Recorded decision: reject');
  });
  it('shows the current correction and keeps the untouched original behind its own disclosure', async () => {
    const editable = {
      ...structuredClone(result),
      provenance: { presentation_mode: 'fixed-part-selection-v33' },
      brief: {
        ...structuredClone(result.brief),
        questions: [{ id: 'R1', text: result.issue }],
        model_uncertainties: [],
        answers: [
          {
            question_id: 'R1',
            status: 'unknown' as const,
            text: 'Original cached draft.',
            snippet_ids: [],
          },
        ],
      },
    };
    await openReviewAnswer(editable);
    api.reviewInvestigation.mockImplementation(async (_id, input) => ({
      ...editable,
      status: 'reviewed',
      review: { ...input },
    }));
    const panel = fillReview('correct');
    fireEvent.change(panel.getByLabelText('Corrected answer'), {
      target: { value: 'Current correction with the original scope retained.' },
    });
    fireEvent.click(panel.getByRole('button', { name: 'Save review' }));
    await screen.findByText('Recorded decision: correct');
    expect(screen.getByText('Current correction with the original scope retained.')).toBeVisible();
    expect(screen.getByText('Original cached draft.')).not.toBeVisible();
    fireEvent.click(screen.getByText('Original draft before correction'));
    expect(screen.getByText('Original cached draft.')).toBeVisible();
    expect(
      api.reviewInvestigation.mock.calls[0]![1].corrected_brief.answers[0].snippet_ids,
    ).toEqual([]);
  });
  it('links only valid selected correction excerpts and keeps original accessible', async () => {
    const citation = 'claim:c1';
    const original = {
      ...structuredClone(result),
      evidence: [
        {
          kind: 'reviewed_claim',
          citation_id: citation,
          content: 'Approval required unless incident response applies.',
        },
        { kind: 'reviewed_claim', citation_id: 'duplicate', content: 'Approval required' },
        { kind: 'reviewed_claim', citation_id: 'duplicate', content: 'Approval required' },
        { kind: 'reviewed_claim', citation_id: 'other', content: 'Approval required' },
      ],
      brief: {
        ...structuredClone(result.brief),
        answers: [{ ...result.brief.answers[0]!, text: 'Original cached draft.' }],
      },
    };
    const corrected = {
      question_id: 'R1',
      status: 'supported',
      text: 'Corrected current prose.',
      snippet_ids: ['S1', 'S2', 'S3', 'S4', 'S5', 'S6'],
      source_excerpts: [
        { snippet_id: 'S1', citation_id: citation, quote: 'Approval required' },
        { snippet_id: 'S2', citation_id: citation, quote: 'incident response' },
        { snippet_id: 'S3', citation_id: 'unknown', quote: 'Approval required' },
        { snippet_id: 'S4', citation_id: citation, quote: 'Not in evidence' },
        { snippet_id: 'S5', citation_id: 'duplicate', quote: 'Approval required' },
        { snippet_id: 'S6', citation_id: citation, quote: '  ' },
        { snippet_id: 'not-selected', citation_id: 'other', quote: 'Approval required' },
      ],
    };
    const saved = {
      ...original,
      status: 'reviewed',
      review: {
        decision: 'correct',
        reviewer: 'Test reviewer',
        reason: 'Checked',
        corrected_brief: { ...original.brief, answers: [corrected] },
      },
    };
    await openReviewAnswer(saved as unknown as typeof result);
    expect(screen.getByText('Corrected current prose.')).toBeVisible();
    expect(within(screen.getByLabelText('Answer sources')).getAllByRole('link')).toHaveLength(1);
    fireEvent.click(within(screen.getByLabelText('Answer sources')).getByRole('link'));
    const evidencePanel = within(screen.getByRole('region', { name: 'Full supplied evidence' }));
    expect(
      evidencePanel.getByText('Approval required unless incident response applies.'),
    ).toBeVisible();
    expect(screen.getByText('Original cached draft.')).not.toBeVisible();
    fireEvent.click(screen.getByText('Original draft before correction'));
    expect(screen.getByText('Original cached draft.')).toBeVisible();
  });
  it('does not fabricate correction links for missing, unselected or invalid evidence', async () => {
    const original = {
      ...structuredClone(result),
      evidence: [
        { kind: 'reviewed_claim', citation_id: 'dup', content: 'exact quote' },
        { kind: 'reviewed_claim', citation_id: 'dup', content: 'exact quote' },
        { kind: 'source', citation_id: 'wrong', content: 'exact quote' },
      ],
    };
    const corrected = {
      question_id: 'R1',
      status: 'supported',
      text: 'Correction with no valid source.',
      snippet_ids: ['s1', 's2', 's3', 's4'],
      source_excerpts: [
        { snippet_id: 's1', citation_id: 'dup', quote: 'exact quote' },
        { snippet_id: 's2', citation_id: 'unknown', quote: 'exact quote' },
        { snippet_id: 's3', citation_id: 'wrong', quote: 'exact quote' },
        { snippet_id: 's4', citation_id: 'dup', quote: '' },
        { snippet_id: 'not-selected', citation_id: 'wrong', quote: 'exact quote' },
      ],
    };
    const saved = {
      ...original,
      status: 'reviewed',
      review: {
        decision: 'correct',
        reviewer: 'Test reviewer',
        reason: 'Checked',
        corrected_brief: { ...original.brief, answers: [corrected] },
      },
    };
    await openReviewAnswer(saved as unknown as typeof result);
    expect(screen.getByText('Correction with no valid source.')).toBeVisible();
    expect(screen.queryByLabelText('Answer sources')).not.toBeInTheDocument();
  });
  it.each([
    ['absent excerpts', undefined, ['s1']],
    ['null excerpt', [null], ['s1']],
    ['non-string quote', [{ snippet_id: 's1', citation_id: 'claim:c1', quote: 7 }], ['s1']],
    ['non-string citation', [{ snippet_id: 's1', citation_id: 7, quote: 'exact quote' }], ['s1']],
    ['no selected ids', [{ snippet_id: 's1', citation_id: 'claim:c1', quote: 'exact quote' }], []],
  ])('does not link %s', async (_name, excerpts, selected) => {
    const original = {
      ...structuredClone(result),
      evidence: [{ kind: 'reviewed_claim', citation_id: 'claim:c1', content: 'exact quote' }],
    };
    const corrected = {
      question_id: 'R1',
      status: 'supported',
      text: 'No linked source.',
      snippet_ids: selected,
      ...(excerpts === undefined ? {} : { source_excerpts: excerpts }),
    };
    const saved = {
      ...original,
      status: 'reviewed',
      review: {
        decision: 'correct',
        reviewer: 'Test reviewer',
        reason: 'Checked',
        corrected_brief: { ...original.brief, answers: [corrected] },
      },
    };
    await openReviewAnswer(saved as unknown as typeof result);
    expect(screen.getByText('No linked source.')).toBeVisible();
    expect(screen.queryByLabelText('Answer sources')).not.toBeInTheDocument();
  });
  it('preserves the answer on export failure and returns the exact canonical Markdown', async () => {
    await openReviewAnswer();
    api.exportInvestigation
      .mockRejectedValueOnce(new Error('unreachable'))
      .mockResolvedValueOnce({ markdown: '# Exact canonical handoff\nwith original references' });
    fireEvent.click(screen.getByRole('button', { name: 'Export Markdown' }));
    await screen.findByText(/Could not complete this action. Your entries are kept/);
    expect(screen.getByRole('region', { name: 'Your answer' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Export Markdown' }));
    expect(await screen.findByLabelText('Exported Markdown')).toHaveValue(
      '# Exact canonical handoff\nwith original references',
    );
    expect(api.exportInvestigation).toHaveBeenLastCalledWith('answer1', {
      workspace: 'w',
      collection_ids: ['c1'],
    });
    expect(api.reviewInvestigation).not.toHaveBeenCalled();
  });
  it('keeps entries but disables review/export after current sources become unavailable', async () => {
    await openReviewAnswer();
    api.reviewInvestigation.mockRejectedValue(new Error('404 unavailable'));
    api.investigation.mockRejectedValue(new Error('404 unavailable'));
    const panel = fillReview();
    fireEvent.click(panel.getByRole('button', { name: 'Save review' }));
    await screen.findByText(/This answer is no longer available from the selected sources/);
    expect(panel.getByLabelText('Reason')).toHaveValue('Checked conditions and source references.');
    expect(panel.getByRole('button', { name: 'Save review' })).toBeDisabled();
    expect(panel.getByRole('button', { name: 'Export Markdown' })).toBeDisabled();
  });
  it('ignores a review reply after the view has unmounted', async () => {
    const view = await openReviewAnswer();
    let resolve!: (value: unknown) => void;
    api.reviewInvestigation.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    fireEvent.click(fillReview().getByRole('button', { name: 'Save review' }));
    view.unmount();
    resolve(reviewed());
    mount();
    await screen.findByRole('checkbox', { name: /Docs/ });
    expect(screen.queryByText('Recorded decision: accept')).not.toBeInTheDocument();
  });
});
