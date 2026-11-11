import { useEffect, useRef, useState } from 'react';
import { localApi } from '@/lib/localApi';
import type { SourceCollection } from '@/types/investigations';
import type { PilotClaim, PilotEvidence, PilotSource } from '@/types/companyBrainPilot';
import { investigationButton as button, investigationField as field } from './controls';

interface Props {
  workspace: string;
  selected: string[];
  collections: SourceCollection[];
  busy: boolean;
  setBusy: (value: boolean) => void;
  refreshCollections: () => Promise<void>;
  invalidateAnswers: () => void;
}

function SourceText({ source }: { source: PilotSource }) {
  return (
    <div className="space-y-1 text-sm">
      <p className="font-medium">
        {source.relative_path} · {source.heading || 'Document'}
      </p>
      <blockquote className="whitespace-pre-wrap border-l-2 border-border pl-3">
        {source.content}
      </blockquote>
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
          Source identity
        </summary>
        <p className="break-all">{source.citation_id}</p>
        <p className="break-all">Revision: {source.revision_id}</p>
      </details>
    </div>
  );
}

export function PilotKnowledge({
  workspace,
  selected,
  collections,
  busy,
  setBusy,
  refreshCollections,
  invalidateAnswers,
}: Props) {
  const [name, setName] = useState('');
  const [folder, setFolder] = useState('');
  const [consent, setConsent] = useState(false);
  const [query, setQuery] = useState('');
  const [evidence, setEvidence] = useState<PilotEvidence | null>(null);
  const [citations, setCitations] = useState<string[]>([]);
  const [claimKey, setClaimKey] = useState('');
  const [content, setContent] = useState('');
  const [claims, setClaims] = useState<PilotClaim[] | null>(null);
  const [proposals, setProposals] = useState<PilotClaim[]>([]);
  const [status, setStatus] = useState<PilotClaim['status']>('proposed');
  const [truncated, setTruncated] = useState(false);
  const [reviewId, setReviewId] = useState('');
  const [reviewer, setReviewer] = useState('');
  const [reason, setReason] = useState('');
  const [replacement, setReplacement] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const disabled = busy || pending;
  const scope = { workspace, collection_ids: selected };
  const ready =
    selected.length > 0 &&
    selected.every((id) => collections.some((c) => c.id === id && c.scan_status === 'ready'));

  async function act(action: () => Promise<void>) {
    if (inFlight.current || busy) return;
    inFlight.current = true;
    setPending(true);
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await action();
    } catch (e) {
      if (alive.current) {
        setError(e instanceof Error ? e.message : 'Could not complete this action. Try again.');
        // A timeout may follow a committed write; re-read before another authority decision.
        setEvidence(null);
        setCitations([]);
        setClaims(null);
        setProposals([]);
        setReviewId('');
        invalidateAnswers();
      }
    } finally {
      inFlight.current = false;
      if (alive.current) {
        setPending(false);
        setBusy(false);
      }
    }
  }
  async function loadClaims(nextStatus = status) {
    const data = await localApi.pilotClaims({ ...scope, status: nextStatus });
    const proposed =
      nextStatus === 'approved'
        ? await localApi.pilotClaims({ ...scope, status: 'proposed' })
        : null;
    if (!alive.current) return;
    setClaims(data.claims);
    setTruncated(data.truncated || !!proposed?.truncated);
    setProposals(proposed?.claims ?? []);
    setReviewId('');
    setReason('');
    setReplacement('');
  }
  function changedEvidence() {
    setEvidence(null);
    setCitations([]);
    setClaims(null);
    setProposals([]);
    setReviewId('');
    invalidateAnswers();
  }

  return (
    <section
      aria-label="Prepare reviewed knowledge"
      aria-busy={pending}
      className="space-y-5 rounded-lg border border-border bg-card p-5"
    >
      <div>
        <h2 className="font-serif text-xl">Prepare reviewed knowledge</h2>
        <p className="text-sm text-muted-foreground">
          Add documents, inspect their evidence, then decide which statements can inform answers.
        </p>
      </div>
      <div className="min-h-6 text-sm">
        {error && (
          <p role="alert" className="text-destructive">
            {error}
          </p>
        )}
        {notice && <p role="status">{notice}</p>}
        {pending && <p role="status">Working locally…</p>}
      </div>
      <details open={collections.length === 0}>
        <summary className="cursor-pointer font-medium hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
          Add a document folder
        </summary>
        <fieldset disabled={disabled} className="mt-3 space-y-3">
          <label className="block text-sm">
            Collection name
            <input
              className={field}
              maxLength={256}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="block text-sm">
            Folder path
            <input
              className={field}
              value={folder}
              onChange={(e) => {
                setFolder(e.target.value);
                setConsent(false);
              }}
            />
          </label>
          <p id="pilot-folder-help" className="text-xs text-muted-foreground">
            Use the full path to a folder on this machine. Scans read Markdown files, excluding
            .git, .obsidian and archive folders. Nothing is uploaded or automatically approved.
          </p>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={consent}
              aria-describedby="pilot-folder-help"
              onChange={(e) => setConsent(e.target.checked)}
            />
            Allow Potluck to read this folder for this collection.
          </label>
          <button
            className={button}
            disabled={disabled || !name.trim() || !folder.trim() || !consent}
            onClick={() =>
              void act(async () => {
                await localApi.registerPilotCollection({
                  workspace,
                  name: name.trim(),
                  folder: folder.trim(),
                  scan_confirmed: true,
                });
                await refreshCollections();
                if (alive.current) {
                  setName('');
                  setFolder('');
                  setConsent(false);
                  setNotice('Folder registered. Scan it below, then select its collection above.');
                }
              })
            }
          >
            Register folder
          </button>
        </fieldset>
      </details>
      {collections.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">Scan documents</h3>
          <p className="text-xs text-muted-foreground">
            Rescanning changed documents invalidates their reviewed knowledge and dependent answers.
            Review the new evidence before approving it again.
          </p>
          {collections.map((c) => (
            <div
              key={c.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-3"
            >
              <span className="text-sm">
                {c.name} <span className="text-muted-foreground">· {c.scan_status}</span>
              </span>
              <button
                className={`${button} min-w-24`}
                disabled={disabled}
                onClick={() =>
                  void act(async () => {
                    changedEvidence();
                    const result = await localApi.scanPilotCollection(c.id, {
                      workspace,
                      scan_confirmed: true,
                    });
                    await refreshCollections();
                    if (alive.current)
                      setNotice(
                        `${c.name}: scanned ${result.files_scanned} files and indexed ${result.chunks_inserted} excerpts.`,
                      );
                  })
                }
              >
                Scan {c.name}
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="space-y-3 border-t border-border pt-4">
        <h3 className="text-sm font-medium">Find source evidence</h3>
        {!ready && (
          <p className="text-sm text-muted-foreground">
            Scan and select your collections above before looking up evidence.
          </p>
        )}
        <label className="block text-sm">
          Search documents
          <input
            className={field}
            disabled={disabled || !ready}
            maxLength={1000}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setEvidence(null);
              setCitations([]);
            }}
          />
        </label>
        <button
          className={button}
          disabled={disabled || !ready || !query.trim()}
          onClick={() =>
            void act(async () => {
              setEvidence(null);
              setCitations([]);
              const data = await localApi.pilotEvidence({ ...scope, query: query.trim() });
              if (alive.current) setEvidence(data);
            })
          }
        >
          Find evidence
        </button>
        {evidence && (
          <div className="space-y-3">
            <p className="text-xs text-muted-foreground">
              {evidence.scope_status.ready} of {evidence.scope_status.requested} selected
              collections ready. Showing {evidence.source_chunks.length} source excerpts.
            </p>
            {evidence.truncated && (
              <p role="status" className="text-sm">
                Results are limited. Refine your search; missing results do not establish that a
                fact is absent.
              </p>
            )}
            {evidence.missing_evidence.map((text, i) => (
              <p key={i} className="text-sm text-muted-foreground">
                {text}
              </p>
            ))}
            {!evidence.source_chunks.length && (
              <p className="text-sm">
                No source excerpts matched. Try terms from the documents or check the selected
                collections.
              </p>
            )}
            {evidence.source_chunks.map((source) => (
              <article
                key={source.citation_id}
                className="space-y-2 rounded-md border border-border p-3"
              >
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    disabled={
                      disabled ||
                      (!citations.includes(source.citation_id) && citations.length >= 16)
                    }
                    checked={citations.includes(source.citation_id)}
                    onChange={(e) =>
                      setCitations(
                        e.target.checked
                          ? [...citations, source.citation_id]
                          : citations.filter((id) => id !== source.citation_id),
                      )
                    }
                  />
                  Use {source.relative_path} as support
                </label>
                <SourceText source={source} />
              </article>
            ))}
          </div>
        )}
        <fieldset disabled={disabled || !ready} className="space-y-3">
          <legend className="text-sm font-medium">Propose knowledge</legend>
          <p className="text-xs text-muted-foreground">
            Write a statement supported by the selected excerpts, retaining its conditions and
            exceptions. Reuse a knowledge key when proposing a replacement. Saving a proposal does
            not approve it.
          </p>
          <label className="block text-sm">
            Knowledge key
            <input
              className={field}
              maxLength={256}
              value={claimKey}
              onChange={(e) => setClaimKey(e.target.value)}
            />
          </label>
          <label className="block text-sm">
            Proposed statement
            <textarea
              className={`${field} resize-none`}
              rows={5}
              maxLength={16000}
              value={content}
              onChange={(e) => setContent(e.target.value)}
            />
          </label>
          <button
            className={button}
            disabled={
              disabled || !ready || !claimKey.trim() || !content.trim() || !citations.length
            }
            onClick={() =>
              void act(async () => {
                await localApi.proposePilotClaim({
                  ...scope,
                  claim_key: claimKey.trim(),
                  content: content.trim(),
                  citation_ids: citations,
                });
                if (!alive.current) return;
                setContent('');
                setCitations([]);
                setStatus('proposed');
                await loadClaims('proposed');
                setNotice('Proposal saved. Inspect its sources below before approving.');
              })
            }
          >
            Save proposal
          </button>
        </fieldset>
      </div>
      <div className="space-y-3 border-t border-border pt-4">
        <h3 className="text-sm font-medium">Review knowledge</h3>
        <p className="text-xs text-muted-foreground">
          This is a local-owner review. A typed name records your decision; it does not authenticate
          a company role. Only current approved statements inform pilot answers.
        </p>
        <label className="block text-sm">
          Knowledge status
          <select
            className={field}
            disabled={disabled}
            value={status}
            onChange={(e) => {
              setStatus(e.target.value as PilotClaim['status']);
              setClaims(null);
              setReviewId('');
              setProposals([]);
            }}
          >
            <option value="proposed">Proposed</option>
            <option value="approved">Approved</option>
            <option value="superseded">Superseded</option>
          </select>
        </label>
        <button
          className={button}
          disabled={disabled || !ready}
          onClick={() => void act(() => loadClaims())}
        >
          Load knowledge
        </button>
        {truncated && claims && (
          <p role="status" className="text-sm">
            This list is limited to 100 statements per status. Narrow your collection selection to
            see other statements.
          </p>
        )}
        {claims?.length === 0 && (
          <p className="text-sm">
            No {status} knowledge in the selected collections. Propose a source-backed statement
            above.
          </p>
        )}
        {claims?.map((claim) => (
          <article key={claim.id} className="space-y-3 rounded-md border border-border p-3">
            <p className="text-sm font-medium">
              {claim.claim_key} · {claim.status}
            </p>
            <p className="whitespace-pre-wrap text-sm">{claim.content}</p>
            {claim.reviewer && (
              <p className="text-xs text-muted-foreground">
                Reviewed by {claim.reviewer}: {claim.reason}
              </p>
            )}
            <details>
              <summary className="cursor-pointer text-sm hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
                Supporting sources ({claim.sources.length})
              </summary>
              <div className="mt-3 space-y-4">
                {claim.sources.map((source) => (
                  <SourceText key={source.citation_id} source={source} />
                ))}
              </div>
            </details>
            {claim.status !== 'superseded' && (
              <button
                className={button}
                disabled={disabled}
                onClick={() => {
                  setReviewId(claim.id);
                  setReason('');
                  setReplacement('');
                }}
              >
                {claim.status === 'proposed' ? 'Review proposal' : 'Replace statement'}
              </button>
            )}
            {reviewId === claim.id && (
              <fieldset disabled={disabled} className="space-y-3 border-t border-border pt-3">
                <legend className="text-sm font-medium">
                  Record a decision for {claim.claim_key}
                </legend>
                {claim.status === 'approved' && (
                  <>
                    <p className="text-sm">
                      Replacing this statement marks it superseded, approves the chosen proposal and
                      invalidates dependent answers.
                    </p>
                    <label className="block text-sm">
                      Replacement proposal
                      <select
                        className={field}
                        value={replacement}
                        onChange={(e) => setReplacement(e.target.value)}
                      >
                        <option value="">Choose a matching proposal</option>
                        {proposals
                          .filter((p) => p.claim_key === claim.claim_key)
                          .map((p) => (
                            <option key={p.id} value={p.id}>
                              {p.content.slice(0, 90)}
                            </option>
                          ))}
                      </select>
                    </label>
                    {proposals
                      .filter((p) => p.claim_key === claim.claim_key && p.id === replacement)
                      .map((p) => (
                        <div key={p.id} className="space-y-2">
                          <p className="whitespace-pre-wrap text-sm">{p.content}</p>
                          {p.sources.map((s) => (
                            <SourceText key={s.citation_id} source={s} />
                          ))}
                        </div>
                      ))}
                  </>
                )}
                <label className="block text-sm">
                  Knowledge reviewer
                  <input
                    className={field}
                    maxLength={256}
                    value={reviewer}
                    onChange={(e) => setReviewer(e.target.value)}
                  />
                </label>
                <label className="block text-sm">
                  Knowledge review reason
                  <textarea
                    className={`${field} resize-none`}
                    rows={3}
                    maxLength={4000}
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </label>
                <div className="flex flex-wrap gap-2">
                  <button
                    className={button}
                    disabled={
                      disabled ||
                      !reviewer.trim() ||
                      !reason.trim() ||
                      (claim.status === 'approved' && !replacement)
                    }
                    onClick={() =>
                      void act(async () => {
                        invalidateAnswers();
                        const input = {
                          ...scope,
                          reviewer: reviewer.trim(),
                          reason: reason.trim(),
                        };
                        if (claim.status === 'proposed')
                          await localApi.approvePilotClaim(claim.id, input);
                        else
                          await localApi.supersedePilotClaim(claim.id, {
                            ...input,
                            replacement_id: replacement,
                          });
                        await loadClaims();
                        if (alive.current)
                          setNotice(
                            claim.status === 'proposed'
                              ? 'Statement approved for pilot answers.'
                              : 'Statement replaced. Dependent answers must be regenerated.',
                          );
                      })
                    }
                  >
                    {claim.status === 'proposed'
                      ? 'Approve statement'
                      : 'Supersede and approve replacement'}
                  </button>
                  <button className={button} onClick={() => setReviewId('')}>
                    Cancel review
                  </button>
                </div>
              </fieldset>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
