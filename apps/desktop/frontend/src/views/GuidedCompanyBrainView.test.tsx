import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { GuidedCompanyBrainView } from './GuidedCompanyBrainView';
import { useCompanyBrainWorkspaceStore } from '@/stores/companyBrainWorkspaceStore';
import type { PilotClaim } from '@/types/companyBrainPilot';
const api = vi.hoisted(() => ({
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
function mount() {
  return render(
    <MemoryRouter>
      <GuidedCompanyBrainView onAdvanced={vi.fn()} />
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
describe('guided Company Brain', () => {
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
    fireEvent.click(screen.getByText('Full supplied evidence'));
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
    expect(screen.getByText(/No model loaded. You can review sources now/)).toBeInTheDocument();
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
