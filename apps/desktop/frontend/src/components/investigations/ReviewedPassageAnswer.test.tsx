import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import {
  ReviewedPassageAnswer,
  AnswerMarkdown,
} from '@/components/investigations/ReviewedPassageAnswer';
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

describe.each([
  'fixed-part-selection-v32',
  'fixed-part-selection-v43',
  'fixed-part-selection-v47',
] as const)('Readable fixed-part answer (%s)', (presentationVersion) => {
  it.each([
    'readable-source-groups-v1',
    'readable-task-statements-v2',
    'readable-task-statements-v3',
  ] as const)('shows %s projection while keeping original evidence accessible', (version) => {
    const value = answer(
      'Draft context.\n\nDraft context.\n\n| Payload | 30 days after fulfilment | TODO |',
    );
    value.passage_presentation!.version = presentationVersion;
    const readable = {
      version,
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
        evidence={[
          {
            kind: 'reviewed_claim',
            citation_id: 'claim:one',
            content: 'Complete source statement.',
          },
        ]}
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
    const fullSource = screen.getByText('Full reviewed statements (1)');
    fireEvent.click(fullSource);
    expect(
      within(fullSource.closest('details')!).getByText('Complete source statement.'),
    ).toBeVisible();
    expect(screen.getAllByRole('link', { name: 'Open claim:one' }).length).toBeGreaterThan(0);
  });

  it.each([
    'readable-source-groups-v1',
    'readable-task-statements-v2',
    'readable-task-statements-v3',
  ] as const)('ignores a stale %s projection', (version) => {
    const value = answer('Corrected condition: always require approval.');
    value.passage_presentation!.version = presentationVersion;
    render(
      <ReviewedPassageAnswer
        answer={value}
        evidence={[]}
        sourceLink={sourceLink}
        readable={{
          version,
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

it('keeps source metadata literal instead of turning it into a setext heading', () => {
  const text =
    '\n---\r\nid: OLT-12\r\nstatus: open\r\nverified: false\r\ncondition: only if both stacks share the account limit\r\n---\n';
  const { container } = render(<AnswerMarkdown text={text} />);
  expect(screen.getByLabelText('Document metadata').textContent).toBe(text);
  expect(container.querySelector('h2')).toBeNull();
});

it('retains ordinary source rules and Markdown headings outside a metadata-only block', () => {
  render(
    <AnswerMarkdown
      text={
        '## Before sending\n\nKeep the ledger guard unless the owner approves an exception.\n\n---'
      }
    />,
  );
  expect(screen.getByRole('heading', { name: 'Before sending' })).toBeVisible();
  expect(
    screen.getByText('Keep the ledger guard unless the owner approves an exception.'),
  ).toBeVisible();
});

it('keeps embedded metadata literal with source rules and citations around it', () => {
  const metadata = '---\nid: OLT-12\nstatus: open\nverified: false\n---';
  const { container } = render(
    <AnswerMarkdown
      text={`Document: email task\n\n${metadata}\n\n## Fix\n\nShare only if both stacks share the account limit.\n\n[Source](https://example.com/evidence)`}
    />,
  );
  expect(container.querySelector('pre')?.textContent?.trim()).toBe(metadata);
  expect(screen.getAllByRole('heading').map((h) => h.textContent)).toEqual(['Fix']);
  expect(screen.getByText('Share only if both stacks share the account limit.')).toBeVisible();
  expect(screen.getByRole('link', { name: 'Source' })).toHaveAttribute(
    'href',
    'https://example.com/evidence',
  );
});

it('leaves metadata examples inside existing code fences as literal code', () => {
  const metadata = '---\nstatus: open\n---';
  const { container } = render(<AnswerMarkdown text={`\`\`\`text\n${metadata}\n\`\`\``} />);
  expect(container.querySelectorAll('pre')).toHaveLength(1);
  expect(container.querySelector('pre')?.textContent?.trim()).toBe(metadata);
});

it('keeps a V34 unresolved clause explicit beside its conditions and citation', () => {
  const text =
    'Not established by the selected reviewed statements (model assessment; review required):\n- P1: what is the deadline?\n\nOnly approved members may book. Suspended members are excluded.';
  const value: Answer = {
    ...answer(text),
    status: 'unknown',
    passage_presentation: {
      version: 'fixed-part-selection-v34',
      answer: text,
      passages: [
        { unit_id: 'C1:whole', text: passage, citation_id: 'claim:one', snippet_ids: ['S1'] },
      ],
    },
  };
  render(
    <ReviewedPassageAnswer
      answer={value}
      evidence={[{ kind: 'reviewed_claim', citation_id: 'claim:one', content: passage }]}
      sourceLink={sourceLink}
    />,
  );
  expect(screen.getAllByText(/what is the deadline/)[0]).toBeVisible();
  expect(screen.getAllByText(passage)[0]).toBeVisible();
  expect(screen.getAllByRole('link', { name: 'Open claim:one' })[0]).toBeVisible();
  expect(screen.getByText('Selected source passages (1)').closest('details')).not.toHaveAttribute(
    'open',
  );
});

it.each(['text', 'status', 'snippets'] as const)(
  'refuses a mismatched V47 %s projection',
  (binding) => {
    const value = answer('Exact condition: only after the clean soak.');
    value.passage_presentation!.version = 'fixed-part-selection-v47';
    const readable = {
      version: 'readable-task-statements-v3' as const,
      original_text: value.text,
      original_status: value.status,
      original_snippet_ids: [...value.snippet_ids],
      shared_context: false,
      markdown: 'Forged projection',
    };
    if (binding === 'text') readable.original_text = 'Other draft';
    if (binding === 'status') readable.original_status = 'unknown';
    if (binding === 'snippets') readable.original_snippet_ids = ['S2'];
    render(
      <ReviewedPassageAnswer
        answer={value}
        readable={readable}
        evidence={[]}
        sourceLink={sourceLink}
      />,
    );
    expect(screen.queryByText('Forged projection')).not.toBeInTheDocument();
    expect(screen.getAllByText(value.text)[0]).toBeVisible();
  },
);
