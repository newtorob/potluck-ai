import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Investigation } from '@/types/investigations';
import { GuidedEvaluationProposal } from './GuidedEvaluationProposal';
import { bindEvaluationProposal } from './evaluationProposal';
const item: Investigation = {
  id: 'answer',
  workspace: 'team',
  collection_ids: ['c2', 'c1'],
  model_slug: 'owned',
  model_sha: 'a'.repeat(64),
  status: 'reviewed',
  issue: 'What should we check?',
  created_at: 'now',
  brief: { facts: [], prior_fixes: [], hypotheses: [], recommended_checks: [] },
  evidence: [{ citation_id: 'claim:two' }, { citation_id: 'claim:one' }],
  review: { decision: 'reject', reviewer: 'Tester', reason: 'Unsupported conclusion.' },
  outcome: { result: 'unresolved', reviewer: 'Observer', notes: 'Still pending.' },
};
function proposal(i = item) {
  return {
    investigation_id: i.id,
    workspace: i.workspace,
    collection_ids: [...i.collection_ids].sort(),
    citation_ids: [...new Set(i.evidence.map((e) => e.citation_id))].sort(),
    review_decision: i.review!.decision,
    outcome_result: i.outcome!.result,
    model_slug: i.model_slug,
    model_sha: i.model_sha,
    status: 'proposed',
  };
}
function acknowledge() {
  fireEvent.click(screen.getByRole('checkbox'));
}
describe('Guided evaluation references', () => {
  it('requires recorded review, outcome and explicit acknowledgement before preparing a rejected-case proposal', async () => {
    const onPrepare = vi.fn().mockResolvedValue(proposal());
    const view = render(
      <GuidedEvaluationProposal investigation={{ ...item, outcome: null }} onPrepare={onPrepare} />,
    );
    expect(screen.getByText(/Record what happened before/)).toBeVisible();
    expect(onPrepare).not.toHaveBeenCalled();
    view.rerender(<GuidedEvaluationProposal investigation={item} onPrepare={onPrepare} />);
    const button = screen.getByRole('button', { name: 'Prepare evaluation proposal' });
    expect(button).toBeDisabled();
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    acknowledge();
    fireEvent.click(button);
    expect(await screen.findByRole('status')).toHaveTextContent('No evaluation has run.');
    const raw = screen.getByLabelText('Evaluation proposal JSON');
    expect(raw).toHaveAttribute('readonly');
    expect(JSON.parse((raw as HTMLTextAreaElement).value)).toEqual(proposal());
    const download = screen.getByRole('link', { name: 'Download evaluation proposal' });
    expect(decodeURIComponent(download.getAttribute('href')!.split(',')[1]!)).toBe(
      JSON.stringify(proposal(), null, 2),
    );
    acknowledge();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(onPrepare).toHaveBeenCalledTimes(1);
  });
  it.each([
    ['investigation_id', 'other'],
    ['workspace', 'other'],
    ['collection_ids', ['c1']],
    ['citation_ids', ['claim:unselected']],
    ['review_decision', 'accept'],
    ['outcome_result', 'resolved'],
    ['model_slug', 'cloud'],
    ['model_sha', 'b'.repeat(64)],
    ['status', 'approved'],
    ['score', 100],
  ])('refuses a proposal with mismatched %s and offers no export', async (key, value) => {
    const onPrepare = vi.fn().mockResolvedValue({ ...proposal(), [key]: value });
    render(<GuidedEvaluationProposal investigation={item} onPrepare={onPrepare} />);
    acknowledge();
    fireEvent.click(screen.getByRole('button', { name: 'Prepare evaluation proposal' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/does not match/);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Evaluation proposal JSON')).not.toBeInTheDocument();
  });
  it('prevents duplicates, ignores a late response across an answer switch, and resets acknowledgement on return', async () => {
    let resolve!: (value: Record<string, unknown>) => void;
    const onPrepare = vi.fn(
      () =>
        new Promise<Record<string, unknown>>((done) => {
          resolve = done;
        }),
    );
    const view = render(<GuidedEvaluationProposal investigation={item} onPrepare={onPrepare} />);
    acknowledge();
    const button = screen.getByRole('button', { name: 'Prepare evaluation proposal' });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(onPrepare).toHaveBeenCalledTimes(1);
    view.rerender(
      <GuidedEvaluationProposal investigation={{ ...item, id: 'next' }} onPrepare={onPrepare} />,
    );
    view.rerender(<GuidedEvaluationProposal investigation={item} onPrepare={onPrepare} />);
    await act(async () => resolve(proposal()));
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
  it('keeps a recoverable failure explicit and only retries on another user action', async () => {
    const onPrepare = vi
      .fn()
      .mockRejectedValueOnce(new Error('Connection unavailable. Reopen the answer.'))
      .mockResolvedValue(proposal());
    render(<GuidedEvaluationProposal investigation={item} onPrepare={onPrepare} />);
    acknowledge();
    fireEvent.click(screen.getByRole('button', { name: 'Prepare evaluation proposal' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Connection unavailable');
    expect(onPrepare).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Prepare evaluation proposal' }));
    await screen.findByRole('link');
    expect(onPrepare).toHaveBeenCalledTimes(2);
  });
  it('does not treat missing model identity or an unreviewed record as a ready test case', () => {
    const onPrepare = vi.fn();
    const missing = { ...item };
    delete missing.model_sha;
    expect(() => bindEvaluationProposal(missing, proposal())).toThrow();
    render(<GuidedEvaluationProposal investigation={missing} onPrepare={onPrepare} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(onPrepare).not.toHaveBeenCalled();
  });
});
