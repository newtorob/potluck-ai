import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PilotKnowledge } from './PilotKnowledge';

const api = vi.hoisted(() => ({
  registerPilotCollection: vi.fn(),
  scanPilotCollection: vi.fn(),
  pilotEvidence: vi.fn(),
  pilotContextPreview: vi.fn(),
  pilotClaims: vi.fn(),
  proposePilotClaim: vi.fn(),
  approvePilotClaim: vi.fn(),
  supersedePilotClaim: vi.fn(),
}));
vi.mock('@/lib/localApi', () => ({ localApi: api }));
const source = {
  citation_id: 'source:chunk:rev1',
  collection_id: 'c1',
  document_id: 'doc',
  chunk_id: 'chunk',
  revision_id: 'rev1',
  relative_path: 'policy.md',
  heading: 'Release',
  content: 'A release needs owner approval unless it is an emergency.',
};
const claim = {
  id: 'p1',
  claim_key: 'release-policy',
  content: source.content,
  status: 'proposed',
  sources: [source],
};
const contextPreview = {
  preview_id: 'preview-one',
  content:
    'Document: Policies / policy.md\n\nTeam policy\n\nDRAFT. Last updated: 2026-01-01.\n\n' +
    source.content,
  citation_ids: ['source:opening:rev1', source.citation_id],
  documents: [
    {
      collection_name: 'Policies',
      collection_id: 'c1',
      document_id: 'doc',
      relative_path: 'policy.md',
      revision_id: 'rev1',
      opening: [],
      excerpts: [source],
    },
  ],
  limitations: ['Indexed snapshot only. Review the full source.'],
};
async function selectEvidence() {
  change('Search documents', 'release');
  fireEvent.click(screen.getByRole('button', { name: 'Find evidence' }));
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Use policy.md as support' }));
}
function props() {
  return {
    workspace: 'w',
    selected: ['c1'],
    collections: [{ id: 'c1', name: 'Policies', scan_status: 'ready', last_scan_at: null }],
    busy: false,
    setBusy: vi.fn(),
    refreshCollections: vi.fn().mockResolvedValue(undefined),
    invalidateAnswers: vi.fn(),
  };
}
const change = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
describe('PilotKnowledge', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.pilotClaims.mockResolvedValue({ claims: [], truncated: false });
    api.pilotEvidence.mockResolvedValue({
      source_chunks: [source],
      truncated: false,
      missing_evidence: [],
      scope_status: { requested: 1, ready: 1 },
    });
    api.pilotContextPreview.mockResolvedValue(contextPreview);
  });
  it('explicitly previews context, preserves an existing draft and includes every context citation without approval', async () => {
    render(<PilotKnowledge {...props()} />);
    await selectEvidence();
    change('Proposed statement', 'Keep this existing statement.');
    change('Knowledge key', 'release');
    expect(api.pilotContextPreview).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Prepare source context' }));
    await screen.findByRole('region', { name: 'Source context preview' });
    expect(api.pilotContextPreview).toHaveBeenCalledWith({
      workspace: 'w',
      collection_ids: ['c1'],
      citation_ids: [source.citation_id],
    });
    expect(screen.getByLabelText('Proposed statement')).toHaveValue(
      'Keep this existing statement.',
    );
    expect(screen.getByLabelText('Prepared source text')).toHaveTextContent(
      'DRAFT. Last updated: 2026-01-01.',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add context and excerpts to draft' }));
    expect(screen.getByLabelText('Proposed statement')).toHaveValue(
      contextPreview.content + '\n\nKeep this existing statement.',
    );
    expect(
      screen.queryByRole('region', { name: 'Source context preview' }),
    ).not.toBeInTheDocument();
    expect(api.proposePilotClaim).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save proposal' }));
    await waitFor(() =>
      expect(api.proposePilotClaim).toHaveBeenCalledWith({
        workspace: 'w',
        collection_ids: ['c1'],
        claim_key: 'release',
        content: contextPreview.content + '\n\nKeep this existing statement.',
        citation_ids: [source.citation_id, 'source:opening:rev1'],
      }),
    );
    expect(api.approvePilotClaim).not.toHaveBeenCalled();
  });
  it('invalidates previews on draft, selection, query and scan changes', async () => {
    const p = props();
    const view = render(<PilotKnowledge {...p} />);
    await selectEvidence();
    const prepare = async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Prepare source context' }));
      await screen.findByRole('region', { name: 'Source context preview' });
    };
    await prepare();
    change('Proposed statement', 'Changed.');
    expect(
      screen.queryByRole('region', { name: 'Source context preview' }),
    ).not.toBeInTheDocument();
    await prepare();
    view.rerender(
      <PilotKnowledge {...p} collections={[{ ...p.collections[0]!, last_scan_at: '2026-01-02' }]} />,
    );
    expect(
      screen.queryByRole('region', { name: 'Source context preview' }),
    ).not.toBeInTheDocument();
    await prepare();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Use policy.md as support' }));
    expect(
      screen.queryByRole('region', { name: 'Source context preview' }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Use policy.md as support' }));
    await prepare();
    change('Search documents', 'Changed query');
    expect(
      screen.queryByRole('region', { name: 'Source context preview' }),
    ).not.toBeInTheDocument();
  });
  it('rejects duplicate preview dispatch and retains the draft after a stale-source failure without retry', async () => {
    let fail!: (e: Error) => void;
    api.pilotContextPreview.mockReturnValue(
      new Promise((_, reject) => {
        fail = reject;
      }),
    );
    render(<PilotKnowledge {...props()} />);
    await selectEvidence();
    change('Proposed statement', 'Keep draft.');
    const button = screen.getByRole('button', { name: 'Prepare source context' });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(api.pilotContextPreview).toHaveBeenCalledOnce();
    expect(button).toBeDisabled();
    fail(new Error('Source changed. Scan and search again.'));
    await screen.findByRole('alert');
    expect(screen.getByLabelText('Proposed statement')).toHaveValue('Keep draft.');
    expect(screen.getByRole('button', { name: 'Save proposal' })).toBeDisabled();
    expect(api.pilotContextPreview).toHaveBeenCalledOnce();
  });
  it('blocks overlong combined drafts instead of trimming context or the existing text', async () => {
    render(<PilotKnowledge {...props()} />);
    await selectEvidence();
    change('Proposed statement', 'x'.repeat(15950));
    fireEvent.click(screen.getByRole('button', { name: 'Prepare source context' }));
    await screen.findByRole('region', { name: 'Source context preview' });
    expect(
      screen.getByRole('button', { name: 'Add context and excerpts to draft' }),
    ).toBeDisabled();
    expect(screen.getByText(/combined draft exceeds 16,000/)).toBeInTheDocument();
    expect(screen.getByLabelText('Proposed statement')).toHaveValue('x'.repeat(15950));
  });
  it('requires folder consent and keeps scanning separate from registration', async () => {
    const p = props();
    render(<PilotKnowledge {...p} collections={[]} selected={[]} />);
    change('Collection name', 'Policies');
    change('Folder path', '/docs');
    expect(screen.getByRole('button', { name: 'Register folder' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /Allow Potluck/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Register folder' }));
    await screen.findByText(/Folder registered/);
    expect(api.registerPilotCollection).toHaveBeenCalledWith({
      workspace: 'w',
      name: 'Policies',
      folder: '/docs',
      scan_confirmed: true,
    });
    expect(api.scanPilotCollection).not.toHaveBeenCalled();
    expect(p.refreshCollections).toHaveBeenCalledOnce();
  });
  it('proposes exact server citation IDs and requires a separate explicit review', async () => {
    api.pilotEvidence.mockResolvedValue({
      source_chunks: [source],
      truncated: true,
      missing_evidence: [],
      scope_status: { requested: 1, ready: 1 },
    });
    api.proposePilotClaim.mockResolvedValue({ claim_id: 'p1' });
    api.pilotClaims.mockResolvedValue({ claims: [claim], truncated: false });
    render(<PilotKnowledge {...props()} />);
    change('Search documents', 'release');
    fireEvent.click(screen.getByRole('button', { name: 'Find evidence' }));
    await screen.findByText(/Results are limited/);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Use policy.md as support' }));
    change('Knowledge key', 'release-policy');
    change('Proposed statement', source.content);
    fireEvent.click(screen.getByRole('button', { name: 'Save proposal' }));
    await screen.findByRole('button', { name: 'Review proposal' });
    expect(api.proposePilotClaim).toHaveBeenCalledWith({
      workspace: 'w',
      collection_ids: ['c1'],
      claim_key: 'release-policy',
      content: source.content,
      citation_ids: ['source:chunk:rev1'],
    });
    expect(api.approvePilotClaim).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Review proposal' }));
    expect(screen.getByRole('button', { name: 'Approve statement' })).toBeDisabled();
    change('Knowledge reviewer', 'Rob');
    change('Knowledge review reason', 'Verified the condition and exception.');
    fireEvent.click(screen.getByRole('button', { name: 'Approve statement' }));
    await waitFor(() =>
      expect(api.approvePilotClaim).toHaveBeenCalledWith('p1', {
        workspace: 'w',
        collection_ids: ['c1'],
        reviewer: 'Rob',
        reason: 'Verified the condition and exception.',
      }),
    );
  });
  it('clears stale source selections on failure, retaining the drafted statement', async () => {
    api.pilotEvidence.mockResolvedValue({
      source_chunks: [source],
      truncated: false,
      missing_evidence: [],
      scope_status: { requested: 1, ready: 1 },
    });
    api.proposePilotClaim.mockRejectedValue(new Error('Source changed. Scan and search again.'));
    const p = props();
    render(<PilotKnowledge {...p} />);
    change('Search documents', 'release');
    fireEvent.click(screen.getByRole('button', { name: 'Find evidence' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Use policy.md as support' }));
    change('Knowledge key', 'release-policy');
    change('Proposed statement', 'Keep this draft');
    fireEvent.click(screen.getByRole('button', { name: 'Save proposal' }));
    await screen.findByRole('alert');
    expect(screen.getByLabelText('Proposed statement')).toHaveValue('Keep this draft');
    expect(screen.getByRole('button', { name: 'Save proposal' })).toBeDisabled();
    expect(p.invalidateAnswers).toHaveBeenCalled();
  });
  it('prevents duplicate scans while pending and invalidates displayed answers', async () => {
    let finish!: (value: { files_scanned: number; chunks_inserted: number }) => void;
    api.scanPilotCollection.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const p = props();
    render(<PilotKnowledge {...p} />);
    fireEvent.click(screen.getByRole('button', { name: 'Scan Policies' }));
    fireEvent.click(screen.getByRole('button', { name: 'Scan Policies' }));
    expect(api.scanPilotCollection).toHaveBeenCalledOnce();
    expect(p.invalidateAnswers).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', { name: 'Scan Policies' })).toBeDisabled();
    finish({ files_scanned: 2, chunks_inserted: 4 });
    await screen.findByText('Policies: scanned 2 files and indexed 4 excerpts.');
  });
  it('requires a matching proposed replacement and records supersession explicitly', async () => {
    api.pilotClaims.mockImplementation(({ status }: { status: string }) =>
      Promise.resolve({
        claims:
          status === 'approved'
            ? [{ ...claim, id: 'old', status }]
            : [claim, { ...claim, id: 'other', claim_key: 'unrelated' }],
        truncated: false,
      }),
    );
    render(<PilotKnowledge {...props()} />);
    change('Knowledge status', 'approved');
    fireEvent.click(screen.getByRole('button', { name: 'Load knowledge' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Replace statement' }));
    expect(screen.getByLabelText('Replacement proposal').querySelectorAll('option')).toHaveLength(
      2,
    );
    change('Knowledge reviewer', 'Rob');
    change('Knowledge review reason', 'Updated policy');
    expect(
      screen.getByRole('button', { name: 'Supersede and approve replacement' }),
    ).toBeDisabled();
    change('Replacement proposal', 'p1');
    fireEvent.click(screen.getByRole('button', { name: 'Supersede and approve replacement' }));
    await waitFor(() =>
      expect(api.supersedePilotClaim).toHaveBeenCalledWith('old', {
        workspace: 'w',
        collection_ids: ['c1'],
        reviewer: 'Rob',
        reason: 'Updated policy',
        replacement_id: 'p1',
      }),
    );
  });
  it('does not deliver late lookup results after the scope unmounts', async () => {
    let finish!: (value: unknown) => void;
    api.pilotEvidence.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const p = props();
    const view = render(<PilotKnowledge {...p} />);
    change('Search documents', 'release');
    fireEvent.click(screen.getByRole('button', { name: 'Find evidence' }));
    view.unmount();
    finish({
      source_chunks: [source],
      truncated: false,
      missing_evidence: [],
      scope_status: { requested: 1, ready: 1 },
    });
    await Promise.resolve();
    expect(p.setBusy).not.toHaveBeenCalledWith(false);
  });
});
