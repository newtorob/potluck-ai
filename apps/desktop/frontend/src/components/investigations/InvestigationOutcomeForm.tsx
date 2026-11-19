import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import type { Investigation } from '@/types/investigations';
import { investigationButton, investigationField } from './controls';

export type OutcomeInput = {
  reviewer: string;
  result: 'resolved' | 'unresolved' | 'inconclusive';
  notes: string;
};

export function InvestigationOutcomeForm({
  investigation,
  disabled = false,
  onSave,
  heading = 'Record actual outcome',
}: {
  investigation: Investigation;
  disabled?: boolean;
  onSave: (input: OutcomeInput) => Promise<void>;
  heading?: string;
}) {
  const id = useId();
  const validScope =
    typeof investigation.workspace === 'string' &&
    !!investigation.workspace.trim() &&
    typeof investigation.id === 'string' &&
    !!investigation.id &&
    Array.isArray(investigation.collection_ids) &&
    investigation.collection_ids.length > 0 &&
    investigation.collection_ids.every((id) => typeof id === 'string' && !!id) &&
    new Set(investigation.collection_ids).size === investigation.collection_ids.length;
  const identity = JSON.stringify([
    investigation.workspace,
    Array.isArray(investigation.collection_ids) ? [...investigation.collection_ids].sort() : null,
    investigation.id,
  ]);
  const current = useRef(identity);
  const generation = useRef(0);
  const alive = useRef(true);
  const locked = useRef(false);
  const resultRef = useRef<HTMLSelectElement>(null);
  const reviewerRef = useRef<HTMLInputElement>(null);
  const notesRef = useRef<HTMLTextAreaElement>(null);
  const [result, setResult] = useState<OutcomeInput['result'] | ''>('');
  const [reviewer, setReviewer] = useState('');
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [error, setError] = useState('');
  if (current.current !== identity) {
    current.current = identity;
    generation.current++;
    locked.current = false;
  }
  useEffect(() => {
    setResult('');
    setReviewer('');
    setNotes('');
    setBusy(false);
    setAttempted(false);
    setError('');
  }, [identity]);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (
      !validScope ||
      locked.current ||
      disabled ||
      busy ||
      !investigation.review ||
      investigation.status !== 'reviewed' ||
      investigation.outcome
    )
      return;
    setAttempted(true);
    if (!result || !reviewer.trim() || !notes.trim()) {
      (!result ? resultRef : !reviewer.trim() ? reviewerRef : notesRef).current?.focus();
      return;
    }
    const submittedIdentity = identity;
    const submittedGeneration = generation.current;
    locked.current = true;
    setBusy(true);
    setError('');
    let saved = false;
    try {
      await onSave({ result, reviewer: reviewer.trim(), notes: notes.trim() });
      saved = true;
    } catch (err) {
      if (
        alive.current &&
        current.current === submittedIdentity &&
        generation.current === submittedGeneration
      )
        setError(
          err instanceof Error ? err.message : 'Could not save the outcome. Your entries are kept.',
        );
    } finally {
      if (
        alive.current &&
        current.current === submittedIdentity &&
        generation.current === submittedGeneration
      ) {
        locked.current = saved;
        setBusy(false);
      }
    }
  }
  return (
    <section aria-label={heading} className="space-y-4 rounded-md border border-border p-4">
      <h3 className="font-serif text-xl">{heading}</h3>
      <p className="text-sm text-muted-foreground">
        Record what happened after the answer was reviewed. This is separate from the review
        decision.
      </p>
      {investigation.outcome ? (
        <div className="space-y-1 text-sm">
          <p>Recorded outcome: {investigation.outcome.result}</p>
          <p>Reviewer: {investigation.outcome.reviewer}</p>
          <p>Notes: {investigation.outcome.notes}</p>
        </div>
      ) : !validScope ? (
        <p className="text-sm">
          Reopen this saved answer in its original workspace and folders to record an outcome.
        </p>
      ) : !investigation.review ? (
        <p className="text-sm">Review this answer first, then return here to record the outcome.</p>
      ) : investigation.status !== 'reviewed' ? (
        <p className="text-sm">
          This investigation is no longer available for recording an outcome.
        </p>
      ) : (
        <form onSubmit={(event) => void submit(event)} noValidate className="space-y-4">
          <fieldset disabled={disabled || busy} className="space-y-4 disabled:opacity-60">
            <div>
              <label htmlFor={`${id}-result`} className="text-sm font-medium">
                Outcome
              </label>
              <select
                ref={resultRef}
                id={`${id}-result`}
                className={investigationField}
                value={result}
                aria-describedby={attempted && !result ? id + '-validation' : undefined}
                aria-invalid={attempted && !result}
                onChange={(e) => setResult(e.target.value as OutcomeInput['result'] | '')}
              >
                <option value="">Choose an outcome</option>
                <option value="resolved">Resolved</option>
                <option value="unresolved">Unresolved</option>
                <option value="inconclusive">Inconclusive</option>
              </select>
            </div>
            <div>
              <label htmlFor={`${id}-reviewer`} className="text-sm font-medium">
                Reviewer name
              </label>
              <input
                ref={reviewerRef}
                id={`${id}-reviewer`}
                className={investigationField}
                maxLength={256}
                value={reviewer}
                aria-describedby={attempted && !reviewer.trim() ? id + '-validation' : undefined}
                aria-invalid={attempted && !reviewer.trim()}
                onChange={(e) => setReviewer(e.target.value)}
              />
            </div>
            <div>
              <label htmlFor={`${id}-notes`} className="text-sm font-medium">
                Outcome notes
              </label>
              <textarea
                ref={notesRef}
                id={`${id}-notes`}
                className={`${investigationField} min-h-24 resize-none`}
                maxLength={4000}
                value={notes}
                aria-describedby={attempted && !notes.trim() ? id + '-validation' : undefined}
                aria-invalid={attempted && !notes.trim()}
                onChange={(e) => setNotes(e.target.value)}
              />
            </div>
            {attempted && (!result || !reviewer.trim() || !notes.trim()) && (
              <p id={id + '-validation'} role="alert" className="text-sm text-destructive">
                Choose an outcome and enter a reviewer name and notes.
              </p>
            )}
            <button type="submit" className={investigationButton + ' min-w-32'} aria-busy={busy}>
              {busy ? 'Saving…' : 'Save outcome'}
            </button>
          </fieldset>
        </form>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </section>
  );
}
