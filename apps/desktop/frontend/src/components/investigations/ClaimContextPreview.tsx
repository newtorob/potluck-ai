import type { PilotContextPreview } from '@/types/companyBrainPilot';
import { investigationButton as button } from './controls';

export function ClaimContextPreview({
  preview,
  disabled,
  error,
  onAdd,
}: {
  preview: PilotContextPreview;
  disabled: boolean;
  error: string;
  onAdd: () => void;
}) {
  return (
    <section aria-label="Source context preview" className="space-y-3 border-t border-border pt-4">
      <h4 className="font-medium">Review source context</h4>
      <p className="text-sm text-muted-foreground">
        These indexed openings may contain dates or draft labels missing from a selected excerpt.
        Review them alongside the excerpts before adding them to your statement.
      </p>
      <ul className="space-y-1 text-sm">
        {preview.documents.map((document) => (
          <li key={`${document.collection_id}:${document.document_id}`}>
            {document.collection_name} / {document.relative_path}
          </li>
        ))}
      </ul>
      <pre
        tabIndex={0}
        aria-label="Prepared source text"
        className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 font-sans text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {preview.content}
      </pre>
      {preview.limitations.map((limitation, i) => (
        <p key={i} className="text-xs text-muted-foreground">
          {limitation}
        </p>
      ))}
      <p className="min-h-5 text-sm text-destructive" role="status">
        {error}
      </p>
      <button className={button} disabled={disabled || !!error} onClick={onAdd}>
        Add context and excerpts to draft
      </button>
      <p className="text-xs text-muted-foreground">
        Adds the source text and its citations before your existing draft. Review conditions and
        exceptions in the full source; this does not save or approve knowledge.
      </p>
    </section>
  );
}
