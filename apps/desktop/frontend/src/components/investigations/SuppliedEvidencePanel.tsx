import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';

export interface SuppliedEvidencePanelHandle {
  openCitation: (citation: string) => void;
}

type Source = Record<string, unknown>;
type Props = { evidence: Source[]; answerId: string };

function text(value: unknown, fallback = ''): string {
  if (value == null) return fallback;
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value) ?? fallback;
  } catch {
    return fallback;
  }
}

function children(item: Source): Source[] {
  return Array.isArray(item.sources)
    ? item.sources.map(
        (value): Source =>
          value && typeof value === 'object' && !Array.isArray(value)
            ? (value as Source)
            : { content: value },
      )
    : [];
}

function label(item: Source, index: number): string {
  const paths = [
    ...new Set(
      children(item)
        .map((source) => text(source.relative_path))
        .filter(Boolean),
    ),
  ];
  return paths.length
    ? paths.join(' · ')
    : text(item.relative_path) || text(item.citation_id) || `Statement ${index + 1}`;
}

export const SuppliedEvidencePanel = forwardRef<SuppliedEvidencePanelHandle, Props>(
  function SuppliedEvidencePanel({ evidence, answerId }, ref) {
    const [view, setView] = useState<{
      answerId: string;
      evidence: Source[];
      opened: number[];
      notice: string;
    }>({ answerId, evidence, opened: [], notice: '' });
    const identity = useRef({ answerId, evidence, generation: 0 });
    if (identity.current.answerId !== answerId || identity.current.evidence !== evidence) {
      identity.current = { answerId, evidence, generation: identity.current.generation + 1 };
    }
    const generation = identity.current.generation;
    const currentView =
      view.answerId === answerId && view.evidence === evidence
        ? view
        : { answerId, evidence, opened: [], notice: '' };
    const sectionRef = useRef<HTMLElement>(null);
    const summaries = useRef<Array<HTMLElement | null>>([]);
    const sourceRefs = useRef<Array<Array<HTMLElement | null>>>([]);
    const alive = useRef(true);
    useEffect(() => {
      alive.current = true;
      return () => {
        alive.current = false;
      };
    }, []);

    useImperativeHandle(
      ref,
      () => ({
        openCitation(citation) {
          const matches: Array<{ index: number; sourceIndex?: number }> = [];
          evidence.forEach((item, index) => {
            const sourceMatches = children(item).flatMap((source, sourceIndex) =>
              source.citation_id === citation ? [sourceIndex] : [],
            );
            if (item.citation_id === citation || sourceMatches.length) {
              matches.push({
                index,
                ...(item.citation_id !== citation && sourceMatches.length === 1
                  ? { sourceIndex: sourceMatches[0] }
                  : {}),
              });
            }
          });
          if (matches.length !== 1) {
            setView({
              answerId,
              evidence,
              opened: [],
              notice:
                'This citation could not be matched to one supplied statement. Review the full inventory below.',
            });
            requestAnimationFrame(() => {
              if (alive.current && identity.current.generation === generation)
                sectionRef.current?.focus();
            });
            return;
          }
          const { index, sourceIndex } = matches[0]!;
          setView({ answerId, evidence, opened: [index], notice: '' });
          requestAnimationFrame(() => {
            if (!alive.current || identity.current.generation !== generation) return;
            if (sourceIndex === undefined) summaries.current[index]?.focus();
            else sourceRefs.current[index]?.[sourceIndex]?.focus();
          });
        },
      }),
      [answerId, evidence, generation],
    );

    return (
      <section
        ref={sectionRef}
        tabIndex={-1}
        id="guided-answer-sources"
        aria-label="Full supplied evidence"
        className="rounded-md border border-border p-3 focus-visible:ring-2 focus-visible:ring-ring"
      >
        <h3 className="font-medium">Full supplied evidence ({evidence.length})</h3>
        <p className="mt-2 text-sm text-muted-foreground">
          Statements supplied to the answer. Some may not be cited in the response.
        </p>
        {currentView.notice && (
          <p role="status" className="mt-2 text-sm">
            {currentView.notice}
          </p>
        )}
        {evidence.length === 0 && <p className="mt-2 text-sm">No supplied evidence.</p>}
        <div className="mt-3 space-y-2">
          {evidence.map((item, index) => (
            <details
              key={`${generation}:${index}`}
              open={currentView.opened.includes(index)}
              onToggle={(event) => {
                if (identity.current.generation !== generation) return;
                const isOpen = event.currentTarget.open;
                setView((previous) => {
                  const old =
                    previous.answerId === answerId && previous.evidence === evidence
                      ? previous
                      : currentView;
                  const opened = isOpen
                    ? old.opened.includes(index)
                      ? old.opened
                      : [...old.opened, index]
                    : old.opened.filter((value) => value !== index);
                  return { answerId, evidence, opened, notice: old.notice };
                });
              }}
              className="border-t border-border pt-2"
            >
              <summary
                ref={(node) => {
                  summaries.current[index] = node;
                }}
                className="cursor-pointer break-words text-sm font-medium hover:text-primary focus-visible:ring-2 focus-visible:ring-ring"
              >
                {label(item, index)}
              </summary>
              <div className="mt-3 space-y-3">
                <p className="break-all text-xs text-muted-foreground">
                  {text(item.citation_id, 'Source')}
                </p>
                <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-sans text-sm">
                  {text(item.content)}
                </pre>
                {children(item).map((source, sourceIndex) => (
                  <div
                    key={sourceIndex}
                    tabIndex={-1}
                    ref={(node) => {
                      (sourceRefs.current[index] ??= [])[sourceIndex] = node;
                    }}
                    className="space-y-1 border-t border-border pt-2 focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <p className="break-words text-xs text-muted-foreground">
                      {[text(source.relative_path), text(source.heading), text(source.citation_id)]
                        .filter(Boolean)
                        .join(' · ')}
                    </p>
                    <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-sans text-sm">
                      {text(source.content)}
                    </pre>
                  </div>
                ))}
              </div>
            </details>
          ))}
        </div>
      </section>
    );
  },
);
