import { createRef } from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SuppliedEvidencePanel, type SuppliedEvidencePanelHandle } from './SuppliedEvidencePanel';

const evidence = [
  {
    citation_id: 'claim:one',
    content: 'Required.\n\nUnless the incident is declared.\n| A | B |',
    sources: [
      {
        citation_id: 'source:one',
        relative_path: 'long/path/policy.md',
        heading: 'Exception',
        content: '<script>unsafe</script>\nOnly after approval.',
      },
    ],
  },
  {
    citation_id: 'claim:two',
    content: { planned: true, count: 3 },
    sources: [
      { citation_id: 'source:two', relative_path: 'other.md', content: 'Uncited complete source.' },
    ],
  },
];

describe('SuppliedEvidencePanel', () => {
  it('shows identities and count before revealing exact complete content', () => {
    render(<SuppliedEvidencePanel answerId="a" evidence={evidence} />);
    const panel = screen.getByRole('region', { name: 'Full supplied evidence' });
    expect(within(panel).getByText('Full supplied evidence (2)')).toBeVisible();
    const summary = within(panel).getByText('long/path/policy.md');
    const details = summary.closest('details')!;
    expect(details).not.toHaveAttribute('open');
    fireEvent.click(summary);
    expect(details).toHaveAttribute('open');
    expect(details.querySelectorAll('pre')[0]).toHaveTextContent(
      'Required. Unless the incident is declared. | A | B |',
    );
    expect(details.querySelectorAll('pre')[0]!.textContent).toBe(evidence[0]!.content);
    expect(details.querySelectorAll('pre')[1]!.textContent).toBe(evidence[0]!.sources[0]!.content);
    expect(details.querySelector('script')).toBeNull();
    fireEvent.click(within(panel).getByText('other.md'));
    expect(within(panel).getByText('{"planned":true,"count":3}')).toBeVisible();
    expect(within(panel).getByText('Uncited complete source.')).toBeVisible();
  });
  it('opens and focuses the exact matching statement for claim and source citations', async () => {
    const ref = createRef<SuppliedEvidencePanelHandle>();
    render(<SuppliedEvidencePanel ref={ref} answerId="a" evidence={evidence} />);
    act(() => ref.current!.openCitation('source:two'));
    await act(() => new Promise((resolve) => requestAnimationFrame(resolve)));
    expect(screen.getByText('other.md').closest('details')).toHaveAttribute('open');
    expect(screen.getByText('other.md · source:two').parentElement).toHaveFocus();
    expect(screen.getByText('long/path/policy.md').closest('details')).not.toHaveAttribute('open');
    act(() => ref.current!.openCitation('claim:one'));
    await act(() => new Promise((resolve) => requestAnimationFrame(resolve)));
    expect(screen.getByText('long/path/policy.md')).toHaveFocus();
  });
  it('falls back for missing or ambiguous citations without selecting a different statement', async () => {
    const ref = createRef<SuppliedEvidencePanelHandle>();
    const duplicate = [evidence[0]!, { ...evidence[1]!, citation_id: 'claim:one' }];
    render(<SuppliedEvidencePanel ref={ref} answerId="a" evidence={duplicate} />);
    for (const citation of ['missing', 'claim:one']) {
      act(() => ref.current!.openCitation(citation));
      await act(() => new Promise((resolve) => requestAnimationFrame(resolve)));
      expect(screen.getByRole('status')).toHaveTextContent('could not be matched');
      expect(screen.getByRole('region', { name: 'Full supplied evidence' })).toHaveFocus();
      expect(screen.getByText('long/path/policy.md').closest('details')).not.toHaveAttribute(
        'open',
      );
      expect(screen.getByText('other.md').closest('details')).not.toHaveAttribute('open');
    }
  });
  it('resets when the answer changes and exposes native keyboard focus', async () => {
    const ref = createRef<SuppliedEvidencePanelHandle>();
    const { rerender } = render(
      <SuppliedEvidencePanel ref={ref} answerId="a" evidence={evidence} />,
    );
    const summary = screen.getByText('long/path/policy.md');
    summary.focus();
    expect(summary).toHaveFocus();
    // jsdom does not implement native summary keyboard activation.
    fireEvent.click(summary);
    expect(summary.closest('details')).toHaveAttribute('open');
    rerender(<SuppliedEvidencePanel ref={ref} answerId="b" evidence={evidence} />);
    expect(screen.getByText('long/path/policy.md').closest('details')).not.toHaveAttribute('open');
    act(() => ref.current!.openCitation('absent'));
    await act(() => new Promise((resolve) => requestAnimationFrame(resolve)));
    rerender(<SuppliedEvidencePanel ref={ref} answerId="c" evidence={[]} />);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByText('No supplied evidence.')).toBeVisible();
  });
  it('deduplicates path labels but keeps every source row and resolves repeated same-statement citations', async () => {
    const ref = createRef<SuppliedEvidencePanelHandle>();
    const repeated = [
      {
        citation_id: 'same',
        content: 'Statement',
        sources: [
          { citation_id: 'same', relative_path: 'path.md', content: 'First chunk' },
          { citation_id: 'same', relative_path: 'path.md', content: 'Second chunk' },
        ],
      },
    ];
    render(<SuppliedEvidencePanel ref={ref} answerId="a" evidence={repeated} />);
    expect(screen.getByText('path.md')).toBeVisible();
    act(() => ref.current!.openCitation('same'));
    await act(() => new Promise((resolve) => requestAnimationFrame(resolve)));
    expect(screen.getByText('path.md')).toHaveFocus();
    expect(screen.getByText('First chunk')).toBeVisible();
    expect(screen.getByText('Second chunk')).toBeVisible();
  });
  it('does not focus or keep open a replacement answer or replaced evidence after a queued citation', () => {
    const queued: FrameRequestCallback[] = [];
    const spy = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => {
      queued.push(cb);
      return 1;
    });
    const ref = createRef<SuppliedEvidencePanelHandle>();
    const { rerender } = render(
      <SuppliedEvidencePanel ref={ref} answerId="a" evidence={evidence} />,
    );
    act(() => ref.current!.openCitation('claim:one'));
    rerender(<SuppliedEvidencePanel ref={ref} answerId="b" evidence={evidence} />);
    act(() => queued.shift()!(0));
    expect(screen.getByText('long/path/policy.md')).not.toHaveFocus();
    expect(screen.getByText('long/path/policy.md').closest('details')).not.toHaveAttribute('open');
    act(() => ref.current!.openCitation('claim:one'));
    rerender(<SuppliedEvidencePanel ref={ref} answerId="b" evidence={[...evidence]} />);
    act(() => queued.shift()!(0));
    expect(screen.getByText('long/path/policy.md')).not.toHaveFocus();
    expect(screen.getByText('long/path/policy.md').closest('details')).not.toHaveAttribute('open');
    spy.mockRestore();
  });
});
