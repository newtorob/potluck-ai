import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { SafeExternalLink } from '@/components/ui/SafeExternalLink';
import type { Brief, ReadableAnswer } from '@/types/investigations';

type Answer = NonNullable<Brief['answers']>[number];

function Markdown({ text }: { text: string }) {
  return (
    <div className="prose prose-sm dark:prose-invert max-w-none break-words text-foreground prose-p:my-2 prose-li:my-0.5 prose-pre:overflow-x-auto prose-pre:bg-muted prose-code:before:content-none prose-code:after:content-none">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          a: SafeExternalLink,
          img: ({ alt }) => <span>{alt || 'Image reference'}</span>,
          table: ({ children }) => (
            <div className="overflow-x-auto">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function recordCells(text: string): string[] | null {
  const lines = text.trim().split('\n');
  const line = lines[0];
  if (lines.length !== 1 || !line || !line.startsWith('|') || !line.endsWith('|')) return null;
  const cells: string[] = [];
  let cell = '';
  let ticks = '';
  const value = line.slice(1, -1);
  for (let i = 0; i < value.length; i += 1) {
    if (value[i] === '\\' && i + 1 < value.length) {
      cell += value.charAt(i) + value.charAt(i + 1);
      i += 1;
    } else if (value[i] === '`') {
      let marker = '`';
      while (value[i + 1] === '`') {
        marker += '`';
        i += 1;
      }
      if (!ticks) ticks = marker;
      else if (ticks === marker) ticks = '';
      cell += marker;
    } else if (value[i] === '|' && !ticks) {
      cells.push(cell.trim());
      cell = '';
    } else cell += value.charAt(i);
  }
  cells.push(cell.trim());
  return cells.length > 1 && !ticks ? cells : null;
}

export function ReviewedPassageAnswer({
  answer,
  evidence,
  sourceLink,
  readable,
}: {
  answer: Answer;
  evidence: Array<Record<string, unknown>>;
  sourceLink: (citationId: string) => ReactNode;
  readable?: ReadableAnswer | undefined;
}) {
  const presentation = answer.passage_presentation;
  if (!presentation) return <Markdown text={answer.text} />;
  const display =
    presentation.version === 'fixed-part-selection-v32' &&
    readable?.version === 'readable-source-groups-v1' &&
    readable.original_text === answer.text &&
    readable.original_status === answer.status &&
    JSON.stringify(readable.original_snippet_ids) === JSON.stringify(answer.snippet_ids)
      ? readable
      : undefined;
  const selected = new Set(presentation.passages.map((passage) => passage.citation_id));
  const originals = evidence.filter(
    (item) => item.kind === 'reviewed_claim' && selected.has(String(item.citation_id)),
  );
  return (
    <div className="space-y-3" aria-label="Selected reviewed passages">
      {presentation.lead && <p className="font-medium">{presentation.lead}</p>}
      {presentation.answer && (
        <Markdown text={display?.markdown ?? (readable ? answer.text : presentation.answer)} />
      )}
      {display?.source_groups ? (
        <div className="flex flex-wrap gap-3 text-xs" aria-label="Evidence citations">
          {display.source_groups.map((group) => (
            <span key={group.evidence_id}>
              Evidence {group.evidence_id}: {sourceLink(group.citation_id)}
            </span>
          ))}
        </div>
      ) : (
        selected.size > 0 && (
          <div className="flex flex-wrap gap-3 text-xs" aria-label="Answer sources">
            {[...selected].map((citation) => (
              <span key={citation}>{sourceLink(citation)}</span>
            ))}
          </div>
        )
      )}
      {display && (
        <details className="rounded-md border border-border p-3">
          <summary className="cursor-pointer text-sm font-medium hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            Original generated answer
          </summary>
          <Markdown text={answer.text} />
        </details>
      )}
      <details className="rounded-md border border-border p-3">
        <summary className="cursor-pointer text-sm font-medium hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          Selected source passages ({presentation.passages.length})
        </summary>
        <div className="mt-3 space-y-3">
          {presentation.passages.map((passage) => {
            const cells = passage.kind === 'table' ? recordCells(passage.text) : null;
            return (
              <div key={passage.unit_id} className="space-y-1">
                {cells ? (
                  <div
                    className="rounded-md border border-border px-3 py-2"
                    aria-label="Selected source record"
                  >
                    <Markdown text={cells[0] || '(Empty cell)'} />
                    <ul className="list-disc pl-5">
                      {cells.slice(1).map((cell, i) => (
                        <li key={i}>
                          <Markdown text={cell || '(Empty cell)'} />
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : (
                  <Markdown text={passage.text} />
                )}
                <div className="text-xs">{sourceLink(passage.citation_id)}</div>
              </div>
            );
          })}
        </div>
      </details>
      <p className="text-xs text-muted-foreground">
        Draft accuracy, selection and completeness need review. These passages cover the selected
        reviewed statements.
      </p>
      {originals.length > 0 && (
        <details className="rounded-md border border-border p-3">
          <summary className="cursor-pointer text-sm font-medium hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            Full reviewed statements ({originals.length})
          </summary>
          <div className="mt-3 space-y-4">
            {originals.map((item) => (
              <div key={String(item.citation_id)}>
                <p className="text-xs font-medium">
                  {String(item.claim_key || 'Reviewed statement')}
                </p>
                <Markdown text={String(item.content || '')} />
                <div className="text-xs">{sourceLink(String(item.citation_id))}</div>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
