import { useState } from 'react';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { localApi } from '@/lib/localApi';
import { useModelStore } from '@/stores/modelStore';
import type { Brief, Investigation, SourceCollection } from '@/types/investigations';

function evidenceText(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function citationLabel(evidence: Record<string, unknown>, index: number): string {
  const path = evidenceText(evidence.relative_path);
  const anchor = evidenceText(evidence.anchor);
  const name = path || (evidenceText(evidence.kind) === 'claim' ? 'Reviewed claim' : 'Source');
  return `[${index + 1}] ${name}${anchor ? ` (${anchor})` : ''}`;
}

const sections = [
  ['facts', 'Facts'],
  ['prior_fixes', 'Prior fixes'],
  ['hypotheses', 'Hypotheses'],
  ['recommended_checks', 'Recommended checks'],
] as const;
const field =
  'w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground';
const button =
  'rounded-md border border-border px-3 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50';

export function InvestigationsView() {
  const activeModel = useModelStore((s) => s.activeModelSlug);
  const fetchModels = useModelStore((s) => s.fetchModels);
  const [workspace, setWorkspace] = useState('');
  const [loadedWorkspace, setLoadedWorkspace] = useState('');
  const [collections, setCollections] = useState<SourceCollection[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [issue, setIssue] = useState('');
  const [query, setQuery] = useState('');
  const [model, setModel] = useState('');
  const [items, setItems] = useState<Investigation[]>([]);
  const [current, setCurrent] = useState<Investigation | null>(null);
  const [decision, setDecision] = useState<'accept' | 'correct' | 'reject'>('accept');
  const [corrected, setCorrected] = useState<Brief | null>(null);
  const [reviewer, setReviewer] = useState('');
  const [reason, setReason] = useState('');
  const [result, setResult] = useState<'' | 'resolved' | 'unresolved' | 'inconclusive'>('');
  const [notes, setNotes] = useState('');
  const [claimKey, setClaimKey] = useState('');
  const [claimContent, setClaimContent] = useState('');
  const [claimCitations, setClaimCitations] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [citationsOpen, setCitationsOpen] = useState(false);
  const scope = { workspace: loadedWorkspace, collection_ids: selected };
  const ready = loadedWorkspace !== '' && selected.length > 0;

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await action();
    } catch (e) {
      setCurrent(null);
      setError(e instanceof Error ? e.message : 'Operation failed');
    } finally {
      setBusy(false);
    }
  }
  async function loadWorkspace() {
    const name = workspace.trim();
    if (!name) {
      setError('Enter a workspace.');
      return;
    }
    setLoadedWorkspace('');
    setSelected([]);
    setCurrent(null);
    setItems([]);
    setCollections([]);
    await run(async () => {
      const data = await localApi.sourceCollections(name);
      setCollections(data.collections);
      setLoadedWorkspace(name);
      await fetchModels();
    });
  }
  async function refresh() {
    const data = await localApi.investigations(scope);
    setItems(data.investigations);
  }
  function show(item: Investigation) {
    setCitationsOpen(false);
    setCurrent(item);
    setCorrected(structuredClone(item.review?.corrected_brief ?? item.brief));
    setDecision('accept');
    setReason('');
    setNotes('');
    setResult('');
    setClaimKey('');
    setClaimContent('');
    setClaimCitations([]);
  }
  async function selectItem(id: string) {
    await run(async () => show(await localApi.investigation(id, scope)));
  }
  function changeText(key: (typeof sections)[number][0], index: number, value: string) {
    setCorrected(
      (old) =>
        old && {
          ...old,
          [key]: old[key].map((item, i) => (i === index ? { ...item, text: value } : item)),
        },
    );
  }
  const effectiveBrief = current?.review?.corrected_brief ?? current?.brief;
  const usedCitations = new Set(
    sections.flatMap(([key]) => effectiveBrief?.[key].flatMap((item) => item.citation_ids) ?? []),
  );
  return (
    <div className="flex h-full flex-col" aria-busy={busy}>
      <ViewHeader
        title="Investigations"
        eyebrow="Company Brain"
        description="Local evidence, a reviewable brief, and a separate recorded outcome."
      />
      <div className="flex-1 overflow-y-auto p-6">
        <div className="mx-auto max-w-5xl space-y-6">
          <p className="text-sm text-muted-foreground">
            Runs on the selected local model. No tools are executed; review does not train a model.
          </p>
          {error && (
            <div role="alert" className="rounded-md border border-destructive p-3 text-sm">
              {error}
            </div>
          )}
          {notice && (
            <div role="status" className="text-sm">
              {notice}
            </div>
          )}
          <section className="rounded-lg border border-border bg-card p-5 space-y-4">
            <h2 className="font-serif text-xl">Choose your sources</h2>
            <div className="flex flex-wrap items-end gap-3">
              <label className="flex-1 text-sm">
                Workspace
                <input
                  className={field}
                  disabled={busy}
                  value={workspace}
                  onChange={(e) => setWorkspace(e.target.value)}
                />
              </label>
              <button className={button} disabled={busy} onClick={() => void loadWorkspace()}>
                Load collections
              </button>
            </div>
            {loadedWorkspace && (
              <>
                <p className="text-xs text-muted-foreground">
                  Workspace: {loadedWorkspace}. Select every collection needed for this
                  investigation.
                </p>
                {collections.length === 0 ? (
                  <p className="text-sm">
                    No collections registered. Register and scan a folder with the local-owner CLI:
                    python -m potluck_local.services.source_collections --workspace WORKSPACE
                    register NAME FOLDER, then scan COLLECTION_ID. Load this workspace again.
                  </p>
                ) : (
                  <fieldset disabled={busy} className="space-y-2">
                    <legend className="text-sm font-medium">Source collections</legend>
                    {collections.map((c) => (
                      <label key={c.id} className="flex items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          checked={selected.includes(c.id)}
                          onChange={(e) => {
                            setSelected(
                              e.target.checked
                                ? [...selected, c.id]
                                : selected.filter((id) => id !== c.id),
                            );
                            setCurrent(null);
                            setItems([]);
                          }}
                        />
                        {c.name}{' '}
                        <span className="text-muted-foreground">
                          {c.scan_status} ·{' '}
                          {c.last_scan_at
                            ? new Date(c.last_scan_at).toLocaleString()
                            : 'never scanned'}
                        </span>
                      </label>
                    ))}
                  </fieldset>
                )}
              </>
            )}
          </section>
          <section className="rounded-lg border border-border bg-card p-5 space-y-4">
            <h2 className="font-serif text-xl">New investigation</h2>
            <label className="block text-sm">
              Issue
              <textarea
                className={`${field} resize-none`}
                rows={3}
                maxLength={4000}
                value={issue}
                onChange={(e) => setIssue(e.target.value)}
              />
            </label>
            <label className="block text-sm">
              Evidence query
              <input
                className={field}
                maxLength={500}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <label className="block text-sm">
              Loaded local model
              <select
                className={field}
                disabled={busy}
                value={model}
                onChange={(e) => setModel(e.target.value)}
              >
                <option value="">Choose the loaded model</option>
                {activeModel && <option value={activeModel}>{activeModel}</option>}
              </select>
            </label>
            {!activeModel && (
              <p className="text-sm text-muted-foreground">
                Load a model in Models, then reload collections to refresh its status.
              </p>
            )}
            <button
              className={button}
              disabled={
                busy || !ready || !issue.trim() || !query.trim() || !model || model !== activeModel
              }
              onClick={() =>
                void run(async () => {
                  const item = await localApi.createInvestigation({
                    ...scope,
                    issue,
                    query,
                    model_slug: model,
                  });
                  show(item);
                  await refresh();
                })
              }
            >
              {busy ? 'Working…' : 'Create investigation'}
            </button>
          </section>
          <section className="rounded-lg border border-border bg-card p-5 space-y-3">
            <div className="flex items-center justify-between gap-4">
              <h2 className="font-serif text-xl">Saved investigations</h2>
              <button
                className={button}
                disabled={busy || !ready}
                onClick={() => void run(refresh)}
              >
                Load saved
              </button>
            </div>
            {items.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                Choose the original collection scope and load saved investigations.
              </p>
            ) : (
              <ul className="space-y-2">
                {items.map((item) => (
                  <li key={item.id}>
                    <button
                      disabled={busy}
                      className="text-left text-sm underline underline-offset-4 disabled:opacity-50"
                      onClick={() => void selectItem(item.id)}
                    >
                      {item.issue} · {item.status}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
          {current && (
            <section className="rounded-lg border border-border bg-card p-5 space-y-5">
              <div>
                <h2 className="font-serif text-xl">{current.issue}</h2>
                <p className="text-xs text-muted-foreground">
                  {current.status} · {current.model_slug} · {current.created_at}
                </p>
              </div>
              {sections.map(([key, label]) => (
                <div key={key}>
                  <h3 className="font-medium">{label}</h3>
                  {(current.review?.corrected_brief ?? current.brief)[key].length === 0 ? (
                    <p className="text-sm text-muted-foreground">None</p>
                  ) : (
                    <ul className="list-disc pl-5 text-sm">
                      {(current.review?.corrected_brief ?? current.brief)[key].map((item, i) => (
                        <li key={i}>
                          {item.text}{' '}
                          {item.citation_ids.map((id) => {
                            const index = current.evidence.findIndex((e) => e.citation_id === id);
                            return index < 0 ? null : (
                              <a
                                key={id}
                                href={`#investigation-citation-${index + 1}`}
                                onClick={(event) => {
                                  event.preventDefault();
                                  setCitationsOpen(true);
                                  requestAnimationFrame(() =>
                                    document
                                      .getElementById(`investigation-citation-${index + 1}`)
                                      ?.scrollIntoView(),
                                  );
                                }}
                                className="ml-1 text-primary underline underline-offset-2"
                                aria-label={`View citation ${index + 1}`}
                              >
                                [{index + 1}]
                              </a>
                            );
                          })}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
              {current.review?.corrected_brief && (
                <details>
                  <summary className="cursor-pointer text-sm">Original brief</summary>
                  {sections.map(([key, label]) => (
                    <div key={key}>
                      <strong>{label}</strong>
                      <ul>
                        {current.brief[key].map((item, i) => (
                          <li key={i}>
                            {item.text} [{item.citation_ids.join(', ')}]
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                  <p>Missing evidence: {current.brief.missing_evidence.join('; ')}</p>
                </details>
              )}
              <div>
                <h3 className="font-medium">Missing evidence</h3>
                <ul className="list-disc pl-5 text-sm">
                  {(current.review?.corrected_brief ?? current.brief).missing_evidence.map(
                    (item, i) => (
                      <li key={i}>{item}</li>
                    ),
                  )}
                </ul>
              </div>
              <details
                open={citationsOpen}
                onToggle={(e) => setCitationsOpen(e.currentTarget.open)}
              >
                <summary className="cursor-pointer font-medium">
                  Inspect citations and source provenance
                </summary>
                <div className="mt-3 space-y-3">
                  {current.evidence.map((e, i) => (
                    <div
                      key={i}
                      id={`investigation-citation-${i + 1}`}
                      className="rounded bg-muted p-3 text-sm space-y-1"
                    >
                      <strong className="break-all">
                        [{i + 1}] {evidenceText(e.citation_id, 'Evidence')} ·{' '}
                        {evidenceText(e.kind, 'source')}
                      </strong>
                      <p className="whitespace-pre-wrap">{evidenceText(e.content)}</p>
                      <p className="break-all text-xs text-muted-foreground">
                        {evidenceText(e.relative_path)} {evidenceText(e.anchor)} · revision{' '}
                        {evidenceText(e.revision_id, 'see sources')} · {evidenceText(e.reviewer)}
                      </p>
                      {Array.isArray(e.sources) && (
                        <ul className="break-all text-xs">
                          {(e.sources as Array<Record<string, unknown>>).map((source, n) => (
                            <li key={n}>
                              {evidenceText(source.relative_path, evidenceText(source.document_id))}{' '}
                              · {evidenceText(source.anchor)} · revision{' '}
                              {evidenceText(source.revision_id)}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  ))}
                </div>
              </details>
              <p className="break-all text-xs text-muted-foreground">
                Model SHA: {current.provenance?.model_sha256 ?? 'unavailable'} · Template:{' '}
                {current.provenance?.template_version ?? 'unavailable'} · Retrieval:{' '}
                {current.provenance?.retrieval_version ?? 'unavailable'}
              </p>
              <button
                className={button}
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    const data = await localApi.exportInvestigation(current.id, scope);
                    const blob = new Blob([data.markdown], { type: 'text/markdown' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `investigation-${current.id}.md`;
                    a.click();
                    URL.revokeObjectURL(url);
                  })
                }
              >
                Export Markdown
              </button>
              <div className="border-t border-border pt-4 space-y-3">
                <h3 className="font-medium">Review the brief</h3>
                <label className="block text-sm">
                  Decision
                  <select
                    className={field}
                    value={decision}
                    onChange={(e) => setDecision(e.target.value as typeof decision)}
                  >
                    <option value="accept">Accept</option>
                    <option value="correct">Correct</option>
                    <option value="reject">Reject</option>
                  </select>
                </label>
                {decision === 'correct' && corrected && (
                  <div className="space-y-3">
                    {sections.map(([key, label]) => (
                      <div key={key}>
                        <h4 className="text-sm font-medium">{label}</h4>
                        {corrected[key].map((item, i) => (
                          <div key={i} className="space-y-1">
                            <label className="block text-xs">
                              Item {i + 1}
                              <textarea
                                className={`${field} resize-none`}
                                rows={2}
                                value={item.text}
                                onChange={(e) => changeText(key, i, e.target.value)}
                              />
                            </label>
                            <fieldset>
                              <legend className="text-xs">Citations</legend>
                              {current.evidence.map((e, index) => {
                                const id = evidenceText(e.citation_id);
                                return (
                                  <label
                                    key={id}
                                    title={id}
                                    className="mr-3 inline-flex items-start gap-1 text-xs"
                                  >
                                    <input
                                      type="checkbox"
                                      checked={item.citation_ids.includes(id)}
                                      onChange={(event) =>
                                        setCorrected({
                                          ...corrected,
                                          [key]: corrected[key].map((old, n) =>
                                            n !== i
                                              ? old
                                              : {
                                                  ...old,
                                                  citation_ids: event.target.checked
                                                    ? [...old.citation_ids, id]
                                                    : old.citation_ids.filter((c) => c !== id),
                                                },
                                          ),
                                        })
                                      }
                                    />
                                    {citationLabel(e, index)}
                                  </label>
                                );
                              })}
                            </fieldset>
                            <button
                              className={button}
                              onClick={() =>
                                setCorrected({
                                  ...corrected,
                                  [key]: corrected[key].filter((_, n) => n !== i),
                                })
                              }
                            >
                              Remove item
                            </button>
                          </div>
                        ))}
                        <button
                          className={button}
                          onClick={() =>
                            setCorrected({
                              ...corrected,
                              [key]: [...corrected[key], { text: '', citation_ids: [] }],
                            })
                          }
                        >
                          Add {label.toLowerCase()} item
                        </button>
                      </div>
                    ))}
                    <label className="block text-sm">
                      Missing evidence, one per line
                      <textarea
                        className={`${field} resize-none`}
                        rows={3}
                        value={corrected.missing_evidence.join('\n')}
                        onChange={(e) =>
                          setCorrected({
                            ...corrected,
                            missing_evidence: e.target.value.split('\n'),
                          })
                        }
                      />
                    </label>
                  </div>
                )}
                <label className="block text-sm">
                  Reviewer
                  <input
                    className={field}
                    maxLength={256}
                    value={reviewer}
                    onChange={(e) => setReviewer(e.target.value)}
                  />
                </label>
                <label className="block text-sm">
                  Reason
                  <textarea
                    className={`${field} resize-none`}
                    rows={3}
                    maxLength={4000}
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </label>
                <button
                  className={button}
                  disabled={
                    busy || current.status !== 'draft' || !reviewer.trim() || !reason.trim()
                  }
                  onClick={() =>
                    void run(async () => {
                      const item = await localApi.reviewInvestigation(current.id, {
                        ...scope,
                        decision,
                        reviewer,
                        reason,
                        ...(decision === 'correct' && corrected
                          ? {
                              corrected_brief: {
                                ...corrected,
                                missing_evidence: corrected.missing_evidence
                                  .map((v) => v.trim())
                                  .filter(Boolean),
                              },
                            }
                          : {}),
                      });
                      show(item);
                      setNotice('Review saved.');
                      await refresh();
                    })
                  }
                >
                  Save review
                </button>
                {current.review && (
                  <p className="text-sm">
                    Review: {current.review.decision} by {current.review.reviewer} ·{' '}
                    {current.review.reason}
                  </p>
                )}
              </div>
              <div className="border-t border-border pt-4 space-y-3">
                <h3 className="font-medium">Record actual outcome</h3>
                <label className="block text-sm">
                  Result
                  <select
                    className={field}
                    value={result}
                    onChange={(e) => setResult(e.target.value as typeof result)}
                  >
                    <option value="">Choose an outcome</option>
                    <option value="resolved">Resolved</option>
                    <option value="unresolved">Unresolved</option>
                    <option value="inconclusive">Inconclusive</option>
                  </select>
                </label>
                <label className="block text-sm">
                  Outcome notes
                  <textarea
                    className={`${field} resize-none`}
                    rows={3}
                    maxLength={4000}
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                  />
                </label>
                <button
                  className={button}
                  disabled={
                    busy ||
                    current.status !== 'reviewed' ||
                    !!current.outcome ||
                    !reviewer.trim() ||
                    !notes.trim() ||
                    !result
                  }
                  onClick={() =>
                    void run(async () => {
                      const item = await localApi.recordInvestigationOutcome(current.id, {
                        ...scope,
                        reviewer,
                        result: result || 'inconclusive',
                        notes,
                      });
                      show(item);
                      setNotice('Outcome saved.');
                    })
                  }
                >
                  Save outcome
                </button>
                {current.outcome && (
                  <p className="text-sm">
                    Outcome: {current.outcome.result} · {current.outcome.notes}
                  </p>
                )}
              </div>
              {current.status === 'reviewed' && current.outcome && (
                <div className="border-t border-border pt-4 space-y-3">
                  <h3 className="font-medium">Propose reuse</h3>
                  <p className="text-sm text-muted-foreground">
                    These actions create proposals for review. They do not approve knowledge or
                    train a model.
                  </p>
                  <button
                    className={button}
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        const data = await localApi.evaluationCase(current.id, scope);
                        const url = URL.createObjectURL(
                          new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
                        );
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = `evaluation-${current.id}.json`;
                        a.click();
                        URL.revokeObjectURL(url);
                        setNotice('Evaluation proposal downloaded.');
                      })
                    }
                  >
                    Create and download evaluation proposal
                  </button>
                  {current.review?.decision !== 'reject' && current.review && (
                    <div className="space-y-3">
                      <label className="block text-sm">
                        Proposed claim key
                        <input
                          className={field}
                          maxLength={256}
                          value={claimKey}
                          onChange={(e) => setClaimKey(e.target.value)}
                        />
                      </label>
                      <label className="block text-sm">
                        Proposed claim
                        <textarea
                          className={`${field} resize-none`}
                          rows={3}
                          maxLength={16000}
                          value={claimContent}
                          onChange={(e) => setClaimContent(e.target.value)}
                        />
                      </label>
                      <fieldset>
                        <legend className="text-sm">Claim citations</legend>
                        {current.evidence
                          .filter((e) => usedCitations.has(evidenceText(e.citation_id)))
                          .map((e) => {
                            const id = evidenceText(e.citation_id);
                            const index = current.evidence.indexOf(e);
                            return (
                              <label
                                key={id}
                                title={id}
                                className="mr-3 inline-flex items-start gap-1 text-xs"
                              >
                                <input
                                  type="checkbox"
                                  checked={claimCitations.includes(id)}
                                  onChange={(event) =>
                                    setClaimCitations(
                                      event.target.checked
                                        ? [...claimCitations, id]
                                        : claimCitations.filter((c) => c !== id),
                                    )
                                  }
                                />
                                {citationLabel(e, index)}
                              </label>
                            );
                          })}
                      </fieldset>
                      <button
                        className={button}
                        disabled={
                          busy || !claimKey.trim() || !claimContent.trim() || !claimCitations.length
                        }
                        onClick={() =>
                          void run(async () => {
                            await localApi.knowledgeProposal(
                              current.id,
                              scope,
                              claimKey,
                              claimContent,
                              claimCitations,
                            );
                            setNotice('Knowledge proposal created for review.');
                          })
                        }
                      >
                        Create knowledge proposal
                      </button>
                    </div>
                  )}
                </div>
              )}
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
