import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import type { Brief, Investigation } from '@/types/investigations';
import { investigationField as inputStyle, investigationButton as buttonStyle } from './controls';
type Verdict = NonNullable<Brief['answers']>[number]['status'];
type Decision = 'accept' | 'correct' | 'reject';
export type GuidedReviewInput = {
  decision: Decision;
  reviewer: string;
  reason: string;
  corrected_brief?: Brief;
};
export function canCorrectGuided(i: Investigation): boolean {
  const q = i.brief.questions ?? [],
    a = i.brief.answers ?? [];
  return (
    [
      'fixed-part-selection-v32',
      'fixed-part-selection-v33',
      'fixed-part-selection-v34',
      'fixed-part-selection-v35',
      'fixed-part-selection-v36',
      'fixed-part-selection-v37',
      'fixed-part-selection-v38',
      'fixed-part-selection-v39',
      'fixed-part-selection-v40',
      'fixed-part-selection-v41',
    ].includes(String(i.provenance?.presentation_mode)) &&
    [
      i.brief.facts,
      i.brief.prior_fixes,
      i.brief.hypotheses,
      i.brief.recommended_checks,
      i.brief.model_uncertainties,
    ].every((items) => Array.isArray(items) && items.length === 0) &&
    q.length === 1 &&
    a.length === 1 &&
    !!q[0]?.id &&
    q[0].id === a[0]?.question_id &&
    ['supported', 'unknown', 'conflicting'].includes(a[0].status) &&
    Array.isArray(a[0].snippet_ids) &&
    a[0].snippet_ids.length <= 100 &&
    a[0].snippet_ids.every((sid) => typeof sid === 'string') &&
    new Set(a[0].snippet_ids).size === a[0].snippet_ids.length &&
    !a[0].calculation &&
    !i.provenance?.calculation_traces?.length &&
    !i.review?.calculation_reviews?.length
  );
}
export function buildGuidedCorrection(i: Investigation, text: string, status: Verdict): Brief {
  if (!canCorrectGuided(i)) throw new Error('This answer needs Advanced investigation.');
  const b = i.brief,
    a = b.answers![0]!;
  if (!text.trim() || text.length > 8000 || (text.trim() === a.text.trim() && status === a.status))
    throw new Error('Change the answer or its verdict before saving.');
  return structuredClone({
    ...b,
    answers: [{ question_id: a.question_id, status, text, snippet_ids: [...a.snippet_ids] }],
  });
}
export function GuidedAnswerReview({
  investigation,
  disabled = false,
  onReview,
  onExport,
  onAdvanced,
  anchorId,
}: {
  investigation: Investigation;
  disabled?: boolean;
  onReview: (input: GuidedReviewInput) => Promise<void>;
  onExport: () => Promise<string>;
  onAdvanced: () => void;
  anchorId?: string;
}) {
  const id = useId();
  const [decision, setDecision] = useState<Decision | ''>(''),
    [reviewer, setReviewer] = useState(''),
    [reason, setReason] = useState('');
  const [text, setText] = useState(investigation.brief.answers?.[0]?.text ?? ''),
    [verdict, setVerdict] = useState<Verdict>(
      investigation.brief.answers?.[0]?.status ?? 'unknown',
    );
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [markdown, setMarkdown] = useState<string | null>(null),
    [copyMessage, setCopyMessage] = useState('');
  const locked = useRef(false),
    eligible = canCorrectGuided(investigation);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const changed =
    text.trim() !== investigation.brief.answers?.[0]?.text.trim() ||
    verdict !== investigation.brief.answers?.[0]?.status;
  const canSave =
    !!decision &&
    !!reviewer.trim() &&
    !!reason.trim() &&
    (decision !== 'correct' || (eligible && !!text.trim() && text.length <= 8000 && changed));
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (locked.current || disabled || busy || !canSave || investigation.status !== 'draft') return;
    locked.current = true;
    setBusy(true);
    setError('');
    try {
      const input: GuidedReviewInput = {
        decision,
        reviewer: reviewer.trim(),
        reason: reason.trim(),
      };
      if (decision === 'correct')
        input.corrected_brief = buildGuidedCorrection(investigation, text, verdict);
      await onReview(input);
    } catch (err) {
      if (alive.current)
        setError(
          err instanceof Error ? err.message : 'Could not save the review. Your entries are kept.',
        );
    } finally {
      locked.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function exportMarkdown() {
    if (locked.current || disabled || busy) return;
    locked.current = true;
    setBusy(true);
    setError('');
    try {
      const exported = await onExport();
      if (!alive.current) return;
      setMarkdown(exported);
      setCopyMessage('');
    } catch (err) {
      if (alive.current)
        setError(err instanceof Error ? err.message : 'Could not export the answer.');
    } finally {
      locked.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function copy() {
    if (disabled || busy || locked.current) return;
    try {
      if (!navigator.clipboard?.writeText || markdown === null) throw new Error('unavailable');
      await navigator.clipboard.writeText(markdown);
      if (alive.current) setCopyMessage('Copied.');
    } catch {
      if (alive.current)
        setCopyMessage('Clipboard unavailable. Select the text and copy it manually.');
    }
  }
  return (
    <section
      id={anchorId}
      tabIndex={-1}
      aria-label="Review and handoff"
      className="space-y-4 rounded-md border border-border p-4"
    >
      <h3 className="font-serif text-xl">Review and handoff</h3>
      <p className="text-xs text-muted-foreground">
        Reviewing or exporting a saved answer does not run a model. The reviewer name is a local
        label, not an authenticated company role.
      </p>
      {investigation.review ? (
        <div className="space-y-1 text-sm">
          <p>Recorded decision: {investigation.review.decision}</p>
          <p>Reviewer: {investigation.review.reviewer}</p>
          <p>Reason: {investigation.review.reason}</p>
        </div>
      ) : investigation.status === 'draft' ? (
        <form onSubmit={(event) => void submit(event)} noValidate className="space-y-4">
          <fieldset disabled={disabled || busy} className="space-y-4 disabled:opacity-60">
            <legend className="text-sm font-medium">Record your review</legend>
            <div>
              <label htmlFor={`${id}-decision`} className="text-sm font-medium">
                Decision
              </label>
              <select
                id={`${id}-decision`}
                className={inputStyle}
                value={decision}
                onChange={(e) => setDecision(e.target.value as Decision | '')}
              >
                <option value="">Choose a decision</option>
                <option value="accept">Accept</option>
                <option value="correct">Correct</option>
                <option value="reject">Reject</option>
              </select>
            </div>
            {decision === 'correct' &&
              (eligible ? (
                <div className="space-y-3">
                  <p className="text-sm text-muted-foreground">
                    Keep the answer's conditions and citations intact. Source selection cannot be
                    changed here.
                  </p>
                  <div>
                    <label htmlFor={`${id}-text`} className="text-sm font-medium">
                      Corrected answer
                    </label>
                    <textarea
                      id={`${id}-text`}
                      className={`${inputStyle} min-h-36 resize-none`}
                      maxLength={8000}
                      value={text}
                      onChange={(e) => setText(e.target.value)}
                    />
                  </div>
                  <div>
                    <label htmlFor={`${id}-verdict`} className="text-sm font-medium">
                      Answer verdict
                    </label>
                    <select
                      id={`${id}-verdict`}
                      className={inputStyle}
                      value={verdict}
                      onChange={(e) => setVerdict(e.target.value as Verdict)}
                    >
                      <option value="supported">Supported</option>
                      <option value="unknown">Unknown</option>
                      <option value="conflicting">Conflicting</option>
                    </select>
                  </div>
                </div>
              ) : (
                <p className="text-sm">This correction needs Advanced investigation.</p>
              ))}
            <div>
              <label htmlFor={`${id}-reviewer`} className="text-sm font-medium">
                Reviewer name
              </label>
              <input
                id={`${id}-reviewer`}
                className={inputStyle}
                maxLength={256}
                value={reviewer}
                onChange={(e) => setReviewer(e.target.value)}
              />
            </div>
            <div>
              <label htmlFor={`${id}-reason`} className="text-sm font-medium">
                Reason
              </label>
              <textarea
                id={`${id}-reason`}
                className={`${inputStyle} min-h-24 resize-none`}
                maxLength={4000}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </div>
            <button type="submit" className={`${buttonStyle} min-w-32`} disabled={!canSave}>
              {busy ? 'Saving…' : 'Save review'}
            </button>
          </fieldset>
        </form>
      ) : (
        <p className="text-sm">
          This investigation cannot be reviewed here. Open Advanced investigation.
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className={buttonStyle}
          disabled={disabled || busy}
          onClick={() => void exportMarkdown()}
        >
          Export Markdown
        </button>
        <button
          type="button"
          className={buttonStyle}
          disabled={disabled || busy}
          onClick={onAdvanced}
        >
          Open Advanced investigation
        </button>
      </div>
      {markdown !== null && (
        <div className="space-y-2">
          <label htmlFor={`${id}-export`} className="text-sm font-medium">
            Exported Markdown
          </label>
          <textarea
            id={`${id}-export`}
            readOnly
            value={markdown}
            onFocus={(e) => e.currentTarget.select()}
            className={`${inputStyle} min-h-40 resize-none`}
          />
          <button
            type="button"
            className={buttonStyle}
            disabled={disabled || busy}
            onClick={() => void copy()}
          >
            Copy Markdown
          </button>
          <p role="status" className="text-sm text-muted-foreground">
            {copyMessage}
          </p>
        </div>
      )}
    </section>
  );
}
