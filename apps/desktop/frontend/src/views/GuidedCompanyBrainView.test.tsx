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
  scanned_at: '2026-11-16T12:00:00Z',
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
  it('requires explicit source approval and question confirmation, then makes exactly one answer request', async () => {
    mount();
    await question();
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
