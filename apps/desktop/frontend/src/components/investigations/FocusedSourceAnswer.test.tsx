import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import {
  FocusedSourceAnswer,
  resolveAnswerOutline,
  type ReviewedAnswerOutline,
} from './FocusedSourceAnswer';
import type { Brief } from '@/types/investigations';

type Answer = NonNullable<Brief['answers']>[number];
const quotes = [
  'Document: Saved brief',
  'Host unlisted only if comments are off.',
  'Show both sides. Wait until confirmation finishes.',
  'Do not show real customer data or filled credentials.',
];
const content = quotes.join('\n\n');
const answer: Answer = {
  question_id: 'R1',
  status: 'supported',
  text: 'Original saved answer with conditions.',
  snippet_ids: ['S1', 'S2', 'S3', 'S4'],
  source_excerpts: quotes.map((quote, i) => ({
    snippet_id: `S${i + 1}`,
    citation_id: 'claim:one',
    quote,
  })),
};
const evidence = [{ kind: 'reviewed_claim', citation_id: 'claim:one', content, sources: [] }];
const plan: ReviewedAnswerOutline = {
  version: 'reviewed-answer-outline-v1',
  original_text: answer.text,
  original_status: answer.status,
  original_snippet_ids: answer.snippet_ids,
  source_contents: { 'claim:one': content },
  sections: [
    { title: 'Hosting', blocks: [{ label: 'Hosting conditions', snippet_ids: ['S2'] }] },
    { title: 'What to show', blocks: [{ label: 'Demonstration', snippet_ids: ['S3'] }] },
    { title: 'What to keep off camera', blocks: [{ label: 'Privacy', snippet_ids: ['S4'] }] },
  ],
  source_label_snippet_ids: ['S1'],
};
const link = (citation: string) => <a href="#source">{citation}</a>;
const copy = () => structuredClone(plan);

describe('FocusedSourceAnswer', () => {
  it('keeps every condition and citation visible under the requested headings, with original closed', () => {
    render(
      <FocusedSourceAnswer answer={answer} evidence={evidence} outline={plan} sourceLink={link} />,
    );
    for (const title of ['Hosting', 'What to show', 'What to keep off camera'])
      expect(screen.getByRole('heading', { name: title })).toBeVisible();
    for (const quote of quotes.slice(1)) expect(screen.getByText(quote)).toBeVisible();
    expect(screen.getAllByRole('link', { name: 'claim:one' })).toHaveLength(3);
    const disclosure = screen.getByText('Full evidence and original answer').closest('details')!;
    expect(disclosure.open).toBe(false);
    fireEvent.click(screen.getByText('Full evidence and original answer'));
    expect(disclosure.open).toBe(true);
    expect(screen.getByText(answer.text)).toBeVisible();
  });
  it.each(['text', 'status', 'snippet_ids'] as const)(
    'falls back after original %s changes',
    (field) => {
      const changed = {
        ...answer,
        ...(field === 'text'
          ? { text: 'Changed original answer' }
          : field === 'status'
            ? { status: 'unknown' as const }
            : { snippet_ids: ['S2', 'S3', 'S4'] }),
      };
      expect(resolveAnswerOutline(changed, evidence, plan)).toBeNull();
      render(
        <FocusedSourceAnswer
          answer={changed}
          evidence={evidence}
          outline={plan}
          sourceLink={link}
        />,
      );
      expect(screen.queryByRole('heading', { name: 'Hosting' })).not.toBeInTheDocument();
      expect(screen.getByText(changed.text)).toBeVisible();
    },
  );
  it('refuses missing coverage, repeated selections and invented citation IDs', () => {
    const missing = copy();
    missing.sections.pop();
    expect(resolveAnswerOutline(answer, evidence, missing)).toBeNull();
    const duplicate = copy();
    duplicate.sections[0]!.blocks[0]!.snippet_ids.push('S2');
    expect(resolveAnswerOutline(answer, evidence, duplicate)).toBeNull();
    const wrong = {
      ...answer,
      source_excerpts: answer.source_excerpts!.map((row) => ({
        ...row,
        citation_id: 'claim:elsewhere',
      })),
    };
    expect(resolveAnswerOutline(wrong, evidence, plan)).toBeNull();
  });
  it('refuses changed quotes and source revisions even if a display plan is present', () => {
    const wrong = {
      ...answer,
      source_excerpts: answer.source_excerpts!.map((row) => ({
        ...row,
        quote: row.quote.replace('only if', 'even if'),
      })),
    };
    expect(resolveAnswerOutline(wrong, evidence, plan)).toBeNull();
    expect(
      resolveAnswerOutline(
        answer,
        [{ ...evidence[0], content: content + '\nNew revision.' }],
        plan,
      ),
    ).toBeNull();
  });
  it('cannot move a condition into the collapsed labels list', () => {
    const wrong = copy();
    wrong.sections[0]!.blocks[0]!.snippet_ids = ['S1'];
    wrong.source_label_snippet_ids = ['S2'];
    expect(resolveAnswerOutline(answer, evidence, wrong)).toBeNull();
    const withConditionalHeading = [{ ...evidence[0], sources: [{ heading: quotes[1] }] }];
    expect(resolveAnswerOutline(answer, withConditionalHeading, wrong)).toBeNull();
  });
  it('falls back instead of crashing on a malformed optional display plan', () => {
    const malformed = { ...plan, sections: null } as unknown as ReviewedAnswerOutline;
    expect(resolveAnswerOutline(answer, evidence, malformed)).toBeNull();
    render(
      <FocusedSourceAnswer
        answer={answer}
        evidence={evidence}
        outline={malformed}
        sourceLink={link}
      />,
    );
    expect(screen.getByText(answer.text)).toBeVisible();
  });

  it('does not mutate the answer, evidence or primary-reviewed display plan', () => {
    const before = JSON.stringify({ answer, evidence, plan });
    expect(resolveAnswerOutline(answer, evidence, plan)).not.toBeNull();
    expect(JSON.stringify({ answer, evidence, plan })).toBe(before);
  });
});
