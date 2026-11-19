import { useEffect, useId, useRef, useState } from 'react';
import type { Investigation } from '@/types/investigations';
import { bindEvaluationProposal } from './evaluationProposal';
import { investigationButton as button, investigationField as field } from './controls';

export function GuidedEvaluationProposal({
  investigation,
  disabled = false,
  onPrepare,
}: {
  investigation: Investigation;
  disabled?: boolean;
  onPrepare: () => Promise<Record<string, unknown>>;
}) {
  const id = useId();
  const binding = JSON.stringify([
    investigation.id,
    investigation.workspace,
    Array.isArray(investigation.collection_ids) ? [...investigation.collection_ids].sort() : null,
    investigation.model_slug,
    investigation.model_sha,
    investigation.brief,
    investigation.evidence,
    investigation.review,
    investigation.outcome,
  ]);
  const context = useRef({ binding, generation: 0 });
  const locked = useRef(false);
  if (context.current.binding !== binding) {
    context.current = { binding, generation: context.current.generation + 1 };
    locked.current = false;
  }
  const token = String(context.current.generation) + ':' + binding;
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [confirmed, setConfirmed] = useState('');
  const [pending, setPending] = useState('');
  const [view, setView] = useState<{
    token: string;
    proposal: ReturnType<typeof bindEvaluationProposal>;
  } | null>(null);
  const [error, setError] = useState<{ token: string; message: string } | null>(null);
  const busy = pending === token;
  const current = () =>
    alive.current &&
    context.current.binding === binding &&
    String(context.current.generation) + ':' + context.current.binding === token;
  const reviewed = investigation.status === 'reviewed' && !!investigation.review;
  const ready =
    Array.isArray(investigation.collection_ids) &&
    investigation.collection_ids.length > 0 &&
    typeof investigation.workspace === 'string' &&
    !!investigation.workspace.trim() &&
    reviewed &&
    !!investigation.outcome &&
    /^[a-f0-9]{64}$/i.test(investigation.model_sha ?? '');
  const proposal = view?.token === token ? view.proposal : null;
  async function prepare() {
    if (locked.current || disabled || !ready || confirmed !== token) return;
    locked.current = true;
    setPending(token);
    setError(null);
    setView(null);
    const snapshot = structuredClone(investigation);
    try {
      const result = await onPrepare();
      if (!current()) return;
      setView({ token, proposal: bindEvaluationProposal(snapshot, result) });
    } catch (cause) {
      if (current())
        setError({
          token,
          message:
            cause instanceof Error
              ? cause.message
              : 'Could not prepare this proposal. Check the connection and reopen the saved answer.',
        });
    } finally {
      if (current()) {
        locked.current = false;
        setPending('');
      }
    }
  }
  return (
    <section
      aria-label="Evaluation proposal"
      className="space-y-3 rounded-lg border border-border p-4"
    >
      <h3 className="font-medium">Keep this case for future evaluation</h3>
      <p className="text-sm text-muted-foreground">
        Prepare a reference to this answer, its recorded review, outcome and sources. A proposal
        still needs a scoring rubric and review before it can test a future change.
      </p>
      {!reviewed ? (
        <p className="text-sm">Review the answer before preparing an evaluation proposal.</p>
      ) : !investigation.outcome ? (
        <p className="text-sm">Record what happened before preparing a proposal.</p>
      ) : !ready ? (
        <p className="text-sm">Reopen this saved answer to load its source and model identities.</p>
      ) : (
        <>
          <p className="text-sm">
            Recorded review: {investigation.review!.decision}. Recorded outcome:{' '}
            {investigation.outcome.result}.
          </p>
          <label className="flex items-start gap-2 text-sm" htmlFor={id + '-confirmed'}>
            <input
              id={id + '-confirmed'}
              type="checkbox"
              className="mt-1"
              disabled={disabled || busy}
              checked={confirmed === token}
              onChange={(e) => {
                setConfirmed(e.target.checked ? token : '');
                setView(null);
                setError(null);
              }}
            />
            I reviewed the recorded outcome and supplied sources for this proposal.
          </label>
          <button
            className={button + ' min-w-52'}
            disabled={disabled || busy || confirmed !== token}
            onClick={() => void prepare()}
            aria-busy={busy}
          >
            {busy ? 'Preparing proposal…' : 'Prepare evaluation proposal'}
          </button>
        </>
      )}
      {error?.token === token && (
        <p role="alert" className="text-sm text-destructive">
          {error.message}
        </p>
      )}
      {proposal && confirmed === token && (
        <div className="space-y-3">
          <p role="status" className="text-sm">
            Proposal prepared for review. No evaluation has run.
          </p>
          <p className="text-xs text-muted-foreground">
            The file contains local record references. It does not include the source documents or a
            passing score.
          </p>
          <label htmlFor={id + '-json'} className="block text-sm font-medium">
            Evaluation proposal JSON
          </label>
          <textarea
            id={id + '-json'}
            readOnly
            className={field + ' min-h-48 resize-none font-mono text-xs'}
            value={JSON.stringify(proposal, null, 2)}
          />
          {!disabled && !busy && (
            <a
              className={button + ' inline-block'}
              download={'evaluation-' + encodeURIComponent(investigation.id) + '.json'}
              href={
                'data:application/json;charset=utf-8,' +
                encodeURIComponent(JSON.stringify(proposal, null, 2))
              }
            >
              Download evaluation proposal
            </a>
          )}
        </div>
      )}
    </section>
  );
}
