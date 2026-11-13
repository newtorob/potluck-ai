import { useId, type ReactNode } from 'react';
import type { Brief, ReadableAnswer, ReviewedAnswerOutline } from '@/types/investigations';
import { AnswerMarkdown, ReviewedPassageAnswer } from './ReviewedPassageAnswer';

type Answer = NonNullable<Brief['answers']>[number];
export type { ReviewedAnswerOutline } from '@/types/investigations';

function resolveBoundOutline(
  answer: Answer,
  evidence: Array<Record<string, unknown>>,
  plan: ReviewedAnswerOutline,
) {
  if (
    plan.version !== 'reviewed-answer-outline-v1' ||
    answer.status !== 'supported' ||
    plan.original_text !== answer.text ||
    plan.original_status !== answer.status ||
    JSON.stringify(plan.original_snippet_ids) !== JSON.stringify(answer.snippet_ids)
  )
    return null;
  const selected = new Set(answer.snippet_ids);
  if (selected.size !== answer.snippet_ids.length || !selected.size || !answer.source_excerpts)
    return null;
  const excerpts = new Map(answer.source_excerpts.map((row) => [row.snippet_id, row]));
  if (
    excerpts.size !== answer.source_excerpts.length ||
    [...selected].some((id) => !excerpts.has(id))
  )
    return null;
  const sources = new Map(
    evidence
      .filter((row) => row.kind === 'reviewed_claim')
      .map((row) => [String(row.citation_id), row]),
  );
  const headings = new Set<string>();
  for (const [citation, content] of Object.entries(plan.source_contents)) {
    const source = sources.get(citation);
    if (!source || source.content !== content) return null;
    for (const item of (source.sources ?? []) as Array<Record<string, unknown>>) {
      if (typeof item.heading === 'string') headings.add(item.heading.trim());
    }
  }
  for (const id of selected) {
    const row = excerpts.get(id)!;
    if (
      !Object.prototype.hasOwnProperty.call(plan.source_contents, row.citation_id) ||
      !row.quote ||
      !plan.source_contents[row.citation_id]!.includes(row.quote)
    )
      return null;
  }
  const used = new Set<string>();
  function take(ids: string[]) {
    if (
      !ids.length ||
      new Set(ids).size !== ids.length ||
      ids.some((id) => !selected.has(id) || used.has(id))
    )
      return null;
    const rows = ids.map((id) => excerpts.get(id)!);
    ids.forEach((id) => used.add(id));
    return rows;
  }
  if (
    !plan.sections.length ||
    new Set(plan.sections.map((s) => s.title)).size !== plan.sections.length
  )
    return null;
  const sections = [];
  const displayedQuotes = new Set<string>();
  for (const section of plan.sections) {
    if (!section.title.trim() || !section.blocks.length) return null;
    const blocks = [];
    for (const block of section.blocks) {
      const rows = take(block.snippet_ids);
      if (!block.label.trim() || !rows) return null;
      rows.forEach((row) => displayedQuotes.add(row.quote.trim()));
      displayedQuotes.add(block.label.trim());
      blocks.push({
        label: block.label,
        text: rows.map((row) => row.quote.trim()).join('\n\n'),
        citations: [...new Set(rows.map((row) => row.citation_id))],
      });
    }
    sections.push({ title: section.title, blocks });
  }
  for (const id of plan.source_label_snippet_ids) {
    const rows = take([id]);
    if (!rows) return null;
    const text = rows[0]!.quote.trim();
    // Only structural labels, blank space, or an exact already-shown duplicate can move below.
    const label =
      headings.has(text) &&
      !/\b(if|unless|only|must|except|until|before|after|when|provided|without)\b|\d/i.test(text);
    const documentLabel = /^Document: [^\n]+$/.test(text);
    if (text && !label && !documentLabel && !displayedQuotes.has(text)) return null;
  }
  return used.size === selected.size ? sections : null;
}

export function resolveAnswerOutline(...args: Parameters<typeof resolveBoundOutline>) {
  try {
    return resolveBoundOutline(...args);
  } catch {
    return null;
  }
}

export function FocusedSourceAnswer({
  answer,
  evidence,
  readable,
  outline,
  sourceLink,
  fullEvidenceId,
}: {
  answer: Answer;
  evidence: Array<Record<string, unknown>>;
  readable?: ReadableAnswer | undefined;
  outline?: ReviewedAnswerOutline | undefined;
  sourceLink: (citation: string) => ReactNode;
  fullEvidenceId?: string;
}) {
  const id = useId();
  const sections = outline ? resolveAnswerOutline(answer, evidence, outline) : null;
  if (!sections)
    return (
      <ReviewedPassageAnswer
        answer={answer}
        evidence={evidence}
        readable={readable}
        sourceLink={sourceLink}
      />
    );
  return (
    <div className="space-y-5" aria-label="Focused source-backed answer">
      <nav aria-label="Answer sections" className="flex flex-wrap gap-3 text-sm">
        {sections.map((section, index) => (
          <a
            key={section.title}
            className="cursor-pointer underline hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            href={`#${id}-${index}`}
          >
            {section.title}
          </a>
        ))}
      </nav>
      {sections.map((section, index) => (
        <section
          key={section.title}
          aria-labelledby={`${id}-${index}`}
          className="space-y-4 rounded-md border border-border p-4"
        >
          <h3 id={`${id}-${index}`} className="font-serif text-2xl">
            {section.title}
          </h3>
          {section.blocks.map((block, blockIndex) => (
            <div key={`${block.label}-${blockIndex}`} className="space-y-2">
              <h4 className="text-sm font-medium">{block.label}</h4>
              <AnswerMarkdown text={block.text} />
              <div
                className="flex flex-wrap gap-3 text-xs"
                aria-label={`Sources for ${block.label}`}
              >
                {block.citations.map((citation) => (
                  <span key={citation}>{sourceLink(citation)}</span>
                ))}
              </div>
            </div>
          ))}
        </section>
      ))}
      <details id={fullEvidenceId} className="rounded-md border border-border p-4">
        <summary className="cursor-pointer font-medium hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          Full evidence and original answer
        </summary>
        <div className="mt-4">
          <ReviewedPassageAnswer
            answer={answer}
            evidence={evidence}
            readable={readable}
            sourceLink={sourceLink}
          />
        </div>
      </details>
    </div>
  );
}
