import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ReviewedPassageAnswer } from '@/components/investigations/ReviewedPassageAnswer';
import type { Brief } from '@/types/investigations';

type Answer = NonNullable<Brief['answers']>[number];
const passage = 'Only approved members may book. Suspended members are excluded.';
function answer(text = passage, kind = 'text', lead = ''): Answer {
  return {
    question_id: 'R1',
    status: lead ? 'unknown' : 'supported',
    text,
    snippet_ids: ['S1'],
    passage_presentation: {
      version: 'reviewed-claim-selection-v3',
      lead,
      answer: text,
      passages: [{ unit_id: 'U1', kind, text, citation_id: 'claim:one', snippet_ids: ['S1'] }],
    },
  };
}
const sourceLink = (cid: string) => (
  <a href="#source" aria-label={`Open ${cid}`}>
    Source 1
  </a>
);

describe('ReviewedPassageAnswer', () => {
  it('shows selected conditions and keeps complete original statements in a closed disclosure', () => {
    render(
      <ReviewedPassageAnswer
        answer={answer()}
        sourceLink={sourceLink}
        evidence={[
          {
            kind: 'reviewed_claim',
            citation_id: 'claim:one',
            claim_key: 'booking',
            content: `${passage}\n\nOther full-statement context.`,
          },
          { kind: 'reviewed_claim', citation_id: 'claim:other', content: 'Unselected source.' },
        ]}
      />,
    );
    expect(screen.getAllByText(passage)[0]).toBeInTheDocument();
    expect(screen.queryByText('Unselected source.')).not.toBeInTheDocument();
    const summary = screen.getByText('Full reviewed statements (1)');
    const disclosure = summary.closest('details')!;
    expect(disclosure.open).toBe(false);
    fireEvent.click(summary);
    expect(disclosure.open).toBe(true);
    expect(screen.getByText('Other full-statement context.')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'Open claim:one' })).toHaveLength(3);
  });

  it('reads a standalone source row without dropping escaped or inline-code pipes', () => {
    render(
      <ReviewedPassageAnswer
        answer={answer(
          '| Suppression | `a|b` | Retain opt-outs unless the tenant is removed. | A \\| B |',
          'table',
        )}
        evidence={[]}
        sourceLink={sourceLink}
      />,
    );
    fireEvent.click(screen.getByText('Selected source passages (1)'));
    expect(screen.getByRole('list')).toBeInTheDocument();
    expect(
      within(screen.getByLabelText('Selected source record')).getByText('a|b'),
    ).toBeInTheDocument();
    expect(
      within(screen.getByLabelText('Selected source record')).getByText(
        'Retain opt-outs unless the tenant is removed.',
      ),
    ).toBeInTheDocument();
    expect(
      within(screen.getByLabelText('Selected source record')).getByText('A | B'),
    ).toBeInTheDocument();
  });

  it('shows explicit unknown scope and blocks active markup and image loading', () => {
    const text =
      '![private image](https://example.invalid/image) [unsafe](file:///secret) <script>bad()</script>';
    const { container } = render(
      <ReviewedPassageAnswer
        answer={answer(text, 'text', 'The selected statements do not specify the requested fact.')}
        evidence={[]}
        sourceLink={sourceLink}
      />,
    );
    expect(
      screen.getByText('The selected statements do not specify the requested fact.'),
    ).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(screen.queryByRole('link', { name: 'unsafe' })).not.toBeInTheDocument();
  });
});

describe('Readable fixed-part answer', () => {
  it('shows shared context and labelled fields while keeping the original closed and accessible', () => {
    const value = answer(
      'Draft context.\n\nDraft context.\n\n| Payload | 30 days after fulfilment | TODO |',
    );
    value.passage_presentation!.version = 'fixed-part-selection-v32';
    const readable = {
      version: 'readable-source-groups-v1' as const,
      original_text: value.text,
      original_status: value.status,
      original_snippet_ids: value.snippet_ids,
      shared_context: true,
      source_groups: [{ evidence_id: '2', citation_id: 'claim:one' }],
      markdown:
        '### Shared source context\nDraft context.\n\n### Evidence 1\n- **Deadline:** 30 days after fulfilment\n- **Status:** TODO',
    };
    render(
      <ReviewedPassageAnswer
        answer={value}
        readable={readable}
        evidence={[]}
        sourceLink={sourceLink}
      />,
    );
    expect(screen.getByRole('heading', { name: 'Shared source context' })).toBeVisible();
    expect(screen.getByText('Deadline:')).toBeVisible();
    expect(screen.getByLabelText('Evidence citations')).toHaveTextContent('Evidence 2: Source 1');
    expect(screen.getByText('30 days after fulfilment')).toBeVisible();
    const summary = screen.getByText('Original generated answer');
    expect(summary.closest('details')).not.toHaveAttribute('open');
    fireEvent.click(summary);
    expect(summary.closest('details')).toHaveAttribute('open');
    expect(within(summary.closest('details')!).getByText(/Payload/)).toBeVisible();
    expect(screen.getAllByRole('link', { name: 'Open claim:one' }).length).toBeGreaterThan(0);
  });

  it('ignores a projection from before a correction', () => {
    const value = answer('Corrected condition: always require approval.');
    value.passage_presentation!.version = 'fixed-part-selection-v32';
    render(
      <ReviewedPassageAnswer
        answer={value}
        evidence={[]}
        sourceLink={sourceLink}
        readable={{
          version: 'readable-source-groups-v1',
          original_text: 'Earlier draft',
          original_status: value.status,
          original_snippet_ids: value.snippet_ids,
          shared_context: true,
          markdown: 'Stale answer',
        }}
      />,
    );
    expect(screen.queryByText('Stale answer')).not.toBeInTheDocument();
    expect(screen.queryByText('Original generated answer')).not.toBeInTheDocument();
    expect(screen.getAllByText('Corrected condition: always require approval.')[0]).toBeVisible();
  });
});
