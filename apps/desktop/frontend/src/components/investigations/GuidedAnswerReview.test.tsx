import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Investigation } from '@/types/investigations';
import { GuidedAnswerReview, buildGuidedCorrection, canCorrectGuided } from './GuidedAnswerReview';

function fixture(): Investigation {
  return {
    id: 'i',
    workspace: 'w',
    collection_ids: [],
    issue: 'q',
    model_slug: 'm',
    status: 'draft',
    created_at: '',
    evidence: [],
    provenance: { presentation_mode: 'fixed-part-selection-v33' },
    review: null,
    outcome: null,
    brief: {
      questions: [{ id: 'q', text: 'Question?' }],
      answers: [
        {
          question_id: 'q',
          status: 'supported',
          text: 'Original',
          snippet_ids: ['S1', 'S2'],
          source_excerpts: [{ snippet_id: 'S1', citation_id: 'c', quote: 'x' }],
        },
      ],
      facts: [],
      prior_fixes: [],
      hypotheses: [],
      recommended_checks: [],
      model_uncertainties: [],
    },
  };
}
function setup(
  i = fixture(),
  review = vi.fn().mockResolvedValue(undefined),
  exp = vi.fn().mockResolvedValue('# Export'),
) {
  render(
    <GuidedAnswerReview investigation={i} onReview={review} onExport={exp} onAdvanced={vi.fn()} />,
  );
  return { review, exp };
}
function fill(decision: string) {
  fireEvent.change(screen.getByLabelText('Decision'), { target: { value: decision } });
  fireEvent.change(screen.getByLabelText('Reviewer name'), { target: { value: 'Rob' } });
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Reviewed' } });
}
describe('GuidedAnswerReview', () => {
  it.each(['accept', 'reject'])('requires explicit inputs and sends %s', async (decision) => {
    const { review } = setup();
    expect(screen.getByLabelText('Decision')).toHaveValue('');
    expect(screen.getByLabelText('Reviewer name')).toHaveValue('');
    expect(screen.getByLabelText('Reason')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Save review' })).toBeDisabled();
    fill(decision);
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    await waitFor(() =>
      expect(review).toHaveBeenCalledWith({ decision, reviewer: 'Rob', reason: 'Reviewed' }),
    );
  });
  it.each(['fixed-part-selection-v33', 'fixed-part-selection-v43', 'fixed-part-selection-v47'])(
    'requires a changed correction and preserves IDs in %s',
    async (mode) => {
      const i = fixture();
      i.provenance = { presentation_mode: mode };
      const { review } = setup(i);
      fill('correct');
      expect(screen.getByRole('button', { name: 'Save review' })).toBeDisabled();
      fireEvent.change(screen.getByLabelText('Corrected answer'), {
        target: { value: 'Corrected only if approved' },
      });
      fireEvent.change(screen.getByLabelText('Answer verdict'), { target: { value: 'unknown' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
      await waitFor(() => expect(review).toHaveBeenCalledOnce());
      const b = review.mock.calls[0]![0].corrected_brief;
      expect(b.answers).toEqual([
        {
          question_id: 'q',
          status: 'unknown',
          text: 'Corrected only if approved',
          snippet_ids: ['S1', 'S2'],
        },
      ]);
      expect(b.questions).toEqual(i.brief.questions);
      expect(b.model_uncertainties).toEqual(i.brief.model_uncertainties);
      expect(i.brief.answers![0]!.text).toBe('Original');
      b.questions[0].text = 'Changed payload';
      b.answers[0].snippet_ids.push('S3');
      b.model_uncertainties.push('new');
      expect(i.brief.questions![0]!.text).toBe('Question?');
      expect(i.brief.answers![0]!.snippet_ids).toEqual(['S1', 'S2']);
      expect(i.brief.model_uncertainties).toEqual([]);
    },
  );
  it('rejects multi-answer, mismatched question and calculation cases', () => {
    const i = fixture();
    expect(canCorrectGuided(i)).toBe(true);
    i.brief.answers![0]!.question_id = 'other';
    expect(canCorrectGuided(i)).toBe(false);
    expect(() => buildGuidedCorrection(i, 'new', 'unknown')).toThrow();
    i.brief.answers![0]!.question_id = 'q';
    i.brief.answers!.push(i.brief.answers![0]!);
    expect(canCorrectGuided(i)).toBe(false);
    i.brief.answers!.pop();
    i.provenance = {
      calculation_traces: [{ request_id: 'r', operation: 'answer', status: 'missing' }],
    };
    expect(canCorrectGuided(i)).toBe(false);
  });
  it('preserves form on failure and prevents concurrent duplicate save', async () => {
    let reject!: (reason: Error) => void;
    const review = vi.fn(
      () =>
        new Promise<void>((_, r) => {
          reject = r;
        }),
    );
    setup(fixture(), review);
    fill('accept');
    const button = screen.getByRole('button', { name: 'Save review' });
    fireEvent.click(button);
    fireEvent.submit(button.closest('form')!);
    expect(review).toHaveBeenCalledTimes(1);
    reject(new Error('Save failed'));
    await screen.findByRole('alert');
    expect(screen.getByLabelText('Reviewer name')).toHaveValue('Rob');
    expect(screen.getByLabelText('Reason')).toHaveValue('Reviewed');
    expect(screen.getByLabelText('Decision')).toHaveValue('accept');
  });
  it('shows recorded review and preserves form values on export failure', async () => {
    const i = fixture();
    i.status = 'reviewed';
    i.review = { decision: 'reject', reviewer: 'Alex', reason: 'Missing evidence' };
    const { unmount } = render(
      <GuidedAnswerReview
        investigation={i}
        onReview={vi.fn()}
        onExport={vi.fn().mockRejectedValue(new Error('Export failed'))}
        onAdvanced={vi.fn()}
      />,
    );
    expect(screen.getByText('Recorded decision: reject')).toBeInTheDocument();
    expect(screen.getByText('Reviewer: Alex')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Export Markdown' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Export failed');
    unmount();
    const { exp } = setup(
      fixture(),
      vi.fn(),
      vi.fn().mockRejectedValue(new Error('Export failed')),
    );
    fill('reject');
    fireEvent.click(screen.getByRole('button', { name: 'Export Markdown' }));
    await waitFor(() => expect(exp).toHaveBeenCalledOnce());
    expect(screen.getByLabelText('Reason')).toHaveValue('Reviewed');
  });
  it('exposes export as selectable read-only Markdown', async () => {
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'Export Markdown' }));
    expect(await screen.findByLabelText('Exported Markdown')).toHaveValue('# Export');
    expect(screen.getByLabelText('Exported Markdown')).toHaveAttribute('readonly');
  });
  it.each([undefined, 'reviewed-claim-selection-v6'])(
    'routes unsupported provenance %s to Advanced',
    (mode) => {
      const i = fixture();
      if (mode) i.provenance = { presentation_mode: mode };
      else delete i.provenance;
      expect(canCorrectGuided(i)).toBe(false);
      setup(i);
      fill('correct');
      expect(screen.getByText('This correction needs Advanced investigation.')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Save review' })).toBeDisabled();
    },
  );
  it('handles a failed export after unmount without updating a later panel', async () => {
    let reject!: (error: Error) => void;
    const pending = new Promise<string>((_, fail) => {
      reject = fail;
    });
    const first = render(
      <GuidedAnswerReview
        investigation={fixture()}
        onReview={vi.fn()}
        onExport={() => pending}
        onAdvanced={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Export Markdown' }));
    first.unmount();
    setup();
    reject(new Error('Old export failed'));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });
  it('reports clipboard failure honestly and blocks copy during a pending export', async () => {
    let resolve!: (value: string) => void;
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const exp = vi
      .fn()
      .mockResolvedValueOnce('# Export')
      .mockImplementationOnce(
        () =>
          new Promise<string>((done) => {
            resolve = done;
          }),
      );
    setup(fixture(), vi.fn(), exp);
    fireEvent.click(screen.getByRole('button', { name: 'Export Markdown' }));
    await screen.findByLabelText('Exported Markdown');
    fireEvent.click(screen.getByRole('button', { name: 'Copy Markdown' }));
    expect(
      await screen.findByText('Clipboard unavailable. Select the text and copy it manually.'),
    ).toBeInTheDocument();
    expect(screen.queryByText('Copied.')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Export Markdown' }));
    const copy = screen.getByRole('button', { name: 'Copy Markdown' });
    expect(copy).toBeDisabled();
    fireEvent.click(copy);
    expect(writeText).toHaveBeenCalledTimes(1);
    resolve('# Second');
    await waitFor(() => expect(screen.getByLabelText('Exported Markdown')).toHaveValue('# Second'));
  });
  it('keeps nonempty legacy sections and malformed citation IDs in Advanced', () => {
    const nonempty = fixture();
    nonempty.brief.model_uncertainties = ['unverified'];
    expect(canCorrectGuided(nonempty)).toBe(false);
    const duplicate = fixture();
    duplicate.brief.answers![0]!.snippet_ids = ['S1', 'S1'];
    expect(canCorrectGuided(duplicate)).toBe(false);
    const before = fixture();
    expect(() =>
      buildGuidedCorrection(before, ' ' + before.brief.answers![0]!.text + ' ', 'supported'),
    ).toThrow();
  });
});
