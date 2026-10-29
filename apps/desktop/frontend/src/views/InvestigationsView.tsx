import { useRef, useState } from 'react';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { localApi } from '@/lib/localApi';
import { useModelStore } from '@/stores/modelStore';
import type {
  Brief,
  Investigation,
  OperationType,
  ReviewedOperation,
  SourceCollection,
} from '@/types/investigations';

function evidenceText(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function citationLabel(evidence: Record<string, unknown>, index: number): string {
  const path = evidenceText(evidence.relative_path);
  const anchor = evidenceText(evidence.anchor);
  const name =
    path || (evidenceText(evidence.kind) === 'reviewed_claim' ? 'Reviewed claim' : 'Source');
  return `[${index + 1}] ${name}${anchor ? ` (${anchor})` : ''}`;
}

const sections = [
  ['facts', 'Facts'],
  ['prior_fixes', 'Prior fixes'],
  ['hypotheses', 'Hypotheses (unverified)'],
  ['recommended_checks', 'Recommended checks'],
] as const;
const field =
  'w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground';
const button =
  'cursor-pointer rounded-md border border-border px-3 py-2 text-sm font-medium hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50';
const operationLabels: Record<OperationType, string> = {
  answer: 'Answer a question',
  difference: 'Difference (after − before)',
  percentage_point_difference: 'Percentage-point difference',
  percent_change: 'Percent change from before',
  percentage: 'Percentage (part of total)',
};

export function InvestigationsView() {
  const activeModel = useModelStore((s) => s.activeModelSlug);
  const fetchModels = useModelStore((s) => s.fetchModels);
  const [workspace, setWorkspace] = useState('');
  const [loadedWorkspace, setLoadedWorkspace] = useState('');
  const [collections, setCollections] = useState<SourceCollection[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [issue, setIssue] = useState('');
  const [query, setQuery] = useState('');
  const [answerEach, setAnswerEach] = useState(false);
  const [reviewedPlan, setReviewedPlan] = useState(false);
  const [operations, setOperations] = useState<ReviewedOperation[]>([
    { request: '', operation: 'answer' },
  ]);
  const [planning, setPlanning] = useState(false);
  const [planNotices, setPlanNotices] = useState<string[]>([]);
  const planRevision = useRef(0);
  const suggestedPlan = useRef(false);
  const [generationProfile, setGenerationProfile] = useState('');
  const [questionsInput, setQuestionsInput] = useState('');
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
  const contentRef = useRef<HTMLDivElement>(null);
  const scope = { workspace: loadedWorkspace, collection_ids: selected };
  const questions = questionsInput
    .split('\n')
    .map((q) => q.trim())
    .filter(Boolean);
  const questionsError = questions.length > 8 || questions.some((q) => q.length > 500);
  const ready = loadedWorkspace !== '' && selected.length > 0;
  const requestErrors = operations.map((row, index) => {
    const request = row.request.trim();
    if (!request) return 'Enter a request.';
    if (request.length > 500) return 'Use at most 500 characters.';
    if (
      operations.some(
        (other, i) => i !== index && other.request.trim().toLowerCase() === request.toLowerCase(),
      )
    )
      return 'Use a distinct request.';
    return '';
  });
  const planValid =
    operations.length > 0 && operations.length <= 12 && requestErrors.every((error) => !error);
  function invalidatePlan() {
    planRevision.current++;
    setPlanNotices([]);
  }
  function invalidatePlanContext() {
    invalidatePlan();
    if (suggestedPlan.current) {
      setOperations([{ request: '', operation: 'answer' }]);
      suggestedPlan.current = false;
    }
  }
  async function suggestPlan() {
    const revision = planRevision.current;
    setPlanning(true);
    setError('');
    setPlanNotices([]);
    try {
      const plan = await localApi.planInvestigation({
        ...scope,
        issue,
        model_slug: model,
        ...(generationProfile ? { generation_profile: generationProfile } : {}),
      });
      if (revision === planRevision.current) {
        setOperations(plan.operations);
        suggestedPlan.current = true;
        setPlanNotices(plan.notices);
      }
    } catch (e) {
      if (revision === planRevision.current)
        setError(e instanceof Error ? e.message : 'Could not suggest a plan.');
    } finally {
      setPlanning(false);
    }
  }

  async function run(action: () => Promise<void>, preserveInvalidReview = false) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await action();
    } catch (e) {
      const invalidReview =
        preserveInvalidReview &&
        e instanceof Error &&
        e.message.startsWith('[localApi] 400: invalid_review_brief:');
      const unavailable =
        e instanceof Error &&
        (/^\[localApi\] (?:401|403|404):/.test(e.message) ||
          /investigation_unavailable|investigation unavailable|evidence unavailable|collections_unavailable/i.test(
            e.message,
          ));
      if (!invalidReview && (!preserveInvalidReview || unavailable)) {
        setCurrent(null);
        setCorrected(null);
      }
      setError(
        invalidReview
          ? 'Check the correction and exact source excerpts, then try again.'
          : e instanceof Error
            ? e.message
            : 'Operation failed',
      );
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
    invalidatePlanContext();
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
    const brief = structuredClone(item.review?.corrected_brief ?? item.brief);
    setCorrected({
      ...brief,
      model_uncertainties: brief.model_uncertainties ?? [],
      ...Object.fromEntries(
        sections.map(([key]) => [
          key,
          brief[key].map((entry) => ({ ...entry, source_excerpts: entry.source_excerpts ?? [] })),
        ]),
      ),
    });
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
  function excerptLink(id: string) {
    if (!current) return null;
    for (let i = 0; i < current.evidence.length; i++) {
      const evidence = current.evidence[i];
      if (!evidence) continue;
      if (evidence.citation_id === id)
        return { target: `investigation-citation-${i + 1}`, label: `Source ${i + 1}` };
      const sources = Array.isArray(evidence.sources)
        ? (evidence.sources as Array<Record<string, unknown>>)
        : [];
      const index = sources.findIndex((source) => source.citation_id === id);
      if (index >= 0)
        return {
          target: `investigation-source-${i + 1}-${index + 1}`,
          label: `Source ${i + 1}.${index + 1}`,
        };
    }
    return null;
  }
  function openCitation(targetId: string) {
    setCitationsOpen(true);
    requestAnimationFrame(() => {
      const target = document.getElementById(targetId);
      const scroller = contentRef.current;
      if (!target || !scroller) return;
      scroller.scrollTop +=
        target.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
      target.focus({ preventScroll: true });
    });
  }
  function sourceLink(id: string) {
    const link = excerptLink(id);
    if (!link) return <span>Source unavailable</span>;
    return (
      <a
        href={`#${link.target}`}
        className="text-primary underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onClick={(event) => {
          event.preventDefault();
          openCitation(link.target);
        }}
      >
        {link.label}
      </a>
    );
  }
  const effectiveBrief = current?.review?.corrected_brief ?? current?.brief;
  const usedCitations = new Set([
    ...sections.flatMap(
      ([key]) => effectiveBrief?.[key].flatMap((item) => item.citation_ids) ?? [],
    ),
    ...(effectiveBrief?.answers?.flatMap(
      (answer) =>
        answer.source_excerpts?.map((excerpt) => {
          const record = current?.evidence.find(
            (e) =>
              e.citation_id === excerpt.citation_id ||
              (Array.isArray(e.sources) &&
                e.sources.some(
                  (source: Record<string, unknown>) => source.citation_id === excerpt.citation_id,
                )),
          );
          return evidenceText(record?.citation_id);
        }) ?? [],
    ) ?? []),
  ]);
  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col" aria-busy={busy}>
      <ViewHeader
        title="Investigations"
        eyebrow="Company Brain"
        description="Local evidence, a reviewable brief, and a separate recorded outcome."
      />
      <div
        ref={contentRef}
        className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden p-6 [overflow-wrap:anywhere]"
      >
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
              <label className="flex-1 text-sm [min-width:min(100%,12rem)]">
                Workspace
                <input
                  className={field}
                  disabled={busy}
                  value={workspace}
                  onChange={(e) => {
                    invalidatePlanContext();
                    setWorkspace(e.target.value);
                  }}
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
                            invalidatePlanContext();
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
                onChange={(e) => {
                  invalidatePlanContext();
                  setIssue(e.target.value);
                }}
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
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                disabled={busy}
                checked={answerEach}
                onChange={(e) => {
                  invalidatePlan();
                  setAnswerEach(e.target.checked);
                  if (e.target.checked) setReviewedPlan(false);
                }}
              />
              Answer each question (preview)
            </label>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                disabled={busy}
                checked={reviewedPlan}
                onChange={(e) => {
                  invalidatePlan();
                  setReviewedPlan(e.target.checked);
                  if (e.target.checked) setAnswerEach(false);
                }}
              />
              Reviewed calculations (experimental)
            </label>
            {reviewedPlan && (
              <div className="space-y-3">
                <p className="text-sm text-muted-foreground">
                  Review each request and calculation type before drafting. You can write your own
                  plan or ask the model for suggestions, which replace the current plan. Confirming
                  the plan does not approve the resulting answers.
                </p>
                <button
                  className={`${button} w-full sm:w-auto sm:min-w-[13rem]`}
                  disabled={
                    busy || planning || !ready || !issue.trim() || !model || model !== activeModel
                  }
                  onClick={() => void suggestPlan()}
                >
                  {planning ? 'Suggesting…' : 'Suggest a plan'}
                </button>
                {planNotices.map((item, i) => (
                  <p key={i} className="text-sm text-muted-foreground">
                    {item}
                  </p>
                ))}
                {operations.map((row, index) => (
                  <div key={index} className="space-y-2 rounded-md border border-border p-3">
                    <label className="block text-sm">
                      Request {index + 1}
                      <textarea
                        className={`${field} resize-none`}
                        rows={2}
                        maxLength={500}
                        disabled={busy}
                        value={row.request}
                        aria-invalid={!!requestErrors[index]}
                        aria-describedby={
                          requestErrors[index] ? `plan-request-error-${index}` : undefined
                        }
                        onChange={(e) => {
                          invalidatePlan();
                          suggestedPlan.current = false;
                          setOperations(
                            operations.map((old, i) =>
                              i === index ? { ...old, request: e.target.value } : old,
                            ),
                          );
                        }}
                      />
                    </label>
                    {requestErrors[index] && (
                      <p id={`plan-request-error-${index}`} className="text-xs text-destructive">
                        {requestErrors[index]}
                      </p>
                    )}
                    <label className="block text-sm">
                      Calculation type {index + 1}
                      <select
                        className={field}
                        disabled={busy}
                        value={row.operation}
                        onChange={(e) => {
                          invalidatePlan();
                          suggestedPlan.current = false;
                          setOperations(
                            operations.map((old, i) =>
                              i === index
                                ? { ...old, operation: e.target.value as OperationType }
                                : old,
                            ),
                          );
                        }}
                      >
                        {(Object.keys(operationLabels) as OperationType[]).map((op) => (
                          <option key={op} value={op}>
                            {operationLabels[op]}
                          </option>
                        ))}
                      </select>
                    </label>
                    <p className="text-xs text-muted-foreground sm:hidden">
                      Selected: {operationLabels[row.operation]}
                    </p>
                    <button
                      className={button}
                      disabled={busy || operations.length <= 1}
                      onClick={() => {
                        invalidatePlan();
                        suggestedPlan.current = false;
                        setOperations(operations.filter((_, i) => i !== index));
                      }}
                    >
                      Remove request {index + 1}
                    </button>
                  </div>
                ))}
                <button
                  className={button}
                  disabled={busy || operations.length >= 12}
                  onClick={() => {
                    invalidatePlan();
                    suggestedPlan.current = false;
                    setOperations([...operations, { request: '', operation: 'answer' }]);
                  }}
                >
                  Add request
                </button>
                {!planValid && (
                  <p className="text-sm text-muted-foreground">
                    Enter 1–12 distinct requests, each up to 500 characters.
                  </p>
                )}
              </div>
            )}
            {(answerEach || reviewedPlan) && (
              <div className="space-y-3">
                {answerEach && (
                  <>
                    <label className="block text-sm">
                      Questions, one per line (optional)
                      <textarea
                        className={`${field} resize-none`}
                        rows={4}
                        disabled={busy}
                        value={questionsInput}
                        aria-invalid={questionsError}
                        aria-describedby="questions-help"
                        onChange={(e) => setQuestionsInput(e.target.value)}
                      />
                      <span id="questions-help" className="text-xs text-muted-foreground">
                        {questionsError
                          ? 'Use up to 8 questions, each at most 500 characters.'
                          : 'Leave blank to plan questions from the issue. Up to 8 questions, 500 characters each.'}
                      </span>
                    </label>
                  </>
                )}
                <label className="block text-sm">
                  Answer profile
                  <select
                    className={field}
                    disabled={busy}
                    value={generationProfile}
                    aria-describedby="answer-profile-help"
                    onChange={(e) => {
                      invalidatePlanContext();
                      setGenerationProfile(e.target.value);
                    }}
                  >
                    <option value="">Standard</option>
                    <option value="qwen3-non-thinking">Qwen3 non-thinking</option>
                  </select>
                </label>
                <p id="answer-profile-help" className="text-xs text-muted-foreground">
                  Qwen3 non-thinking requires a compatible loaded Qwen3 model. If it is
                  incompatible, the request fails without switching profiles.
                </p>
              </div>
            )}
            <label className="block text-sm">
              Loaded local model
              <select
                className={field}
                disabled={busy}
                value={model}
                onChange={(e) => {
                  invalidatePlanContext();
                  setModel(e.target.value);
                }}
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
              className={`${button} ${reviewedPlan ? 'w-full sm:w-auto sm:min-w-[13rem]' : ''}`}
              disabled={
                busy ||
                !ready ||
                !issue.trim() ||
                !query.trim() ||
                !model ||
                model !== activeModel ||
                (answerEach && questionsError) ||
                (reviewedPlan && (!planValid || planning))
              }
              onClick={() =>
                void run(async () => {
                  const item = await localApi.createInvestigation({
                    ...scope,
                    issue,
                    query,
                    model_slug: model,
                    ...(reviewedPlan
                      ? {
                          reviewed_operations: operations.map((row) => ({
                            ...row,
                            request: row.request.trim(),
                          })),
                          plan_confirmed: true,
                          ...(generationProfile ? { generation_profile: generationProfile } : {}),
                        }
                      : {}),
                    ...(answerEach
                      ? {
                          answer_each_question: true,
                          ...(questions.length ? { questions } : {}),
                          ...(generationProfile ? { generation_profile: generationProfile } : {}),
                        }
                      : {}),
                  });
                  show(item);
                  await refresh();
                })
              }
            >
              {busy ? 'Working…' : reviewedPlan ? 'Confirm plan and draft' : 'Create investigation'}
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
              {effectiveBrief?.questions && (
                <div className="space-y-3">
                  <h3 className="font-medium">Questions and answers</h3>
                  <p className="text-xs text-muted-foreground">
                    Draft answers need review. “Supported” is a model claim, not verified
                    correctness. “Unknown” means the presented evidence does not establish an
                    answer.{' '}
                    {current.provenance?.question_origin === 'planned'
                      ? 'Questions were planned from the issue.'
                      : current.provenance?.question_origin === 'explicit'
                        ? 'Questions were provided by you.'
                        : ''}
                  </p>
                  {effectiveBrief.questions.map((question) => {
                    const answer = effectiveBrief.answers?.find(
                      (entry) => entry.question_id === question.id,
                    );
                    return (
                      <div key={question.id} className="space-y-1 text-sm">
                        <h4 className="font-medium">{question.text}</h4>
                        {answer ? (
                          <>
                            <p className="text-xs font-medium">
                              {answer.status === 'supported'
                                ? 'Supported (model claim)'
                                : answer.status === 'conflicting'
                                  ? 'Conflicting'
                                  : 'Unknown'}
                            </p>
                            <p className="whitespace-pre-wrap">{answer.text}</p>
                            {(answer.source_excerpts ?? []).map((excerpt) => (
                              <blockquote
                                key={excerpt.snippet_id}
                                className="border-l-2 border-border pl-3 whitespace-pre-wrap text-muted-foreground"
                              >
                                “{excerpt.quote}”{' '}
                                <span className="text-xs">({sourceLink(excerpt.citation_id)})</span>
                              </blockquote>
                            ))}
                          </>
                        ) : (
                          <p>Answer unavailable</p>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
              {!effectiveBrief?.questions &&
                sections.map(([key, label]) => (
                  <div key={key}>
                    <h3 className="font-medium">{label}</h3>
                    {key === 'facts' && (
                      <p className="text-xs text-muted-foreground">
                        Exact excerpts show source text; they do not establish that a claim is
                        supported.
                      </p>
                    )}
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
                                    openCitation(`investigation-citation-${index + 1}`);
                                  }}
                                  className="ml-1 text-primary underline underline-offset-2"
                                  aria-label={`View citation ${index + 1}`}
                                >
                                  [{index + 1}]
                                </a>
                              );
                            })}
                            {(item.source_excerpts ?? []).map((excerpt, n) => (
                              <blockquote
                                key={n}
                                className="mt-1 border-l-2 border-border pl-3 whitespace-pre-wrap text-muted-foreground"
                              >
                                “{excerpt.quote}”{' '}
                                <span className="text-xs">({sourceLink(excerpt.citation_id)})</span>
                              </blockquote>
                            ))}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
              {current.review?.corrected_brief && (
                <details>
                  <summary className="cursor-pointer text-sm">Original brief</summary>
                  {current.brief.questions?.map((question) => {
                    const answer = current.brief.answers?.find(
                      (entry) => entry.question_id === question.id,
                    );
                    return (
                      <div key={question.id} className="text-sm">
                        <strong>{question.text}</strong>
                        <p>
                          {answer?.status}: {answer?.text}
                        </p>
                        {answer?.source_excerpts?.map((excerpt) => (
                          <blockquote
                            key={excerpt.snippet_id}
                            className="border-l-2 border-border pl-3 whitespace-pre-wrap"
                          >
                            “{excerpt.quote}” ({sourceLink(excerpt.citation_id)})
                          </blockquote>
                        ))}
                      </div>
                    );
                  })}
                  {!current.brief.questions &&
                    sections.map(([key, label]) => (
                      <div key={key}>
                        <strong>{label}</strong>
                        <ul>
                          {current.brief[key].map((item, i) => (
                            <li key={i}>
                              {item.text} [{item.citation_ids.join(', ')}]
                              {(item.source_excerpts ?? []).map((excerpt, n) => (
                                <blockquote
                                  key={n}
                                  className="border-l-2 border-border pl-3 whitespace-pre-wrap"
                                >
                                  “{excerpt.quote}” ({sourceLink(excerpt.citation_id)})
                                </blockquote>
                              ))}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ))}
                  {current.brief.missing_evidence && (
                    <p>
                      Historical combined missing evidence (provenance unknown):{' '}
                      {current.brief.missing_evidence.join('; ')}
                    </p>
                  )}
                  {!!current.brief.model_uncertainties?.length && (
                    <p>
                      Model questions (unverified): {current.brief.model_uncertainties.join('; ')}
                    </p>
                  )}
                </details>
              )}
              {effectiveBrief?.missing_evidence && (
                <div>
                  <h3 className="font-medium">
                    Historical combined missing evidence (provenance unknown)
                  </h3>
                  <ul className="list-disc pl-5 text-sm">
                    {effectiveBrief.missing_evidence.map((item, i) => (
                      <li key={i}>{item}</li>
                    ))}
                  </ul>
                </div>
              )}
              {!!effectiveBrief?.model_uncertainties?.length && (
                <div>
                  <h3 className="font-medium">Model questions (unverified)</h3>
                  <ul className="list-disc pl-5 text-sm">
                    {effectiveBrief.model_uncertainties.map((item, i) => (
                      <li key={i}>{item}</li>
                    ))}
                  </ul>
                </div>
              )}
              {(current.retrieval_notices ?? current.provenance?.retrieval_notices ?? []).length >
                0 && (
                <div>
                  <h3 className="font-medium">Retrieval notices</h3>
                  <ul className="list-disc pl-5 text-sm">
                    {(current.retrieval_notices ?? current.provenance?.retrieval_notices ?? []).map(
                      (item, i) => (
                        <li key={i}>{item}</li>
                      ),
                    )}
                  </ul>
                </div>
              )}
              {!!current.provenance?.extraction_notices?.length && (
                <div className="space-y-2">
                  <h3 className="font-medium">Calculation extraction notices</h3>
                  {current.provenance.extraction_notices.map((item, i) => (
                    <p key={i} className="text-sm">
                      {item.source_text} · {item.reason} ({sourceLink(item.citation_id)})
                    </p>
                  ))}
                </div>
              )}
              {!!current.provenance?.calculation_traces?.length && (
                <div className="space-y-3">
                  <h3 className="font-medium">Original machine calculations</h3>
                  <p className="text-xs text-muted-foreground">
                    These traces belong to the original draft. Corrections do not change the
                    calculation or verify source selection.
                  </p>
                  {current.provenance.calculation_traces.map((trace) => (
                    <div key={trace.request_id} className="space-y-1 text-sm">
                      <h4 className="font-medium">
                        {effectiveBrief?.questions?.find((q) => q.id === trace.request_id)?.text ??
                          trace.request_id}{' '}
                        · {operationLabels[trace.operation]}
                      </h4>
                      <p>
                        Status: {trace.status} · Formula: {trace.formula ?? 'unavailable'}
                      </p>
                      {trace.evaluated_expression && <p>Evaluated: {trace.evaluated_expression}</p>}
                      {trace.result != null && (
                        <p>
                          Result: {trace.result} {trace.unit ?? ''}
                        </p>
                      )}
                      {trace.reason && <p>{trace.reason}</p>}
                      {trace.caveat && <p className="text-muted-foreground">{trace.caveat}</p>}
                      {trace.inputs?.map((input) => (
                        <p key={input.id}>
                          Input {input.id}: {input.source_text} ({sourceLink(input.citation_id)})
                        </p>
                      ))}
                      <details className="text-xs text-muted-foreground">
                        <summary className="cursor-pointer">Calculation details</summary>
                        {trace.algorithm_version && <p>Algorithm: {trace.algorithm_version}</p>}
                        {trace.rounding && (
                          <p>
                            Rounding: {trace.rounding.mode}, up to{' '}
                            {trace.rounding.max_fractional_digits} decimal places
                          </p>
                        )}
                        {trace.inputs
                          ?.filter((input) => input.unit_source_text)
                          .map((input) => (
                            <p key={input.id}>
                              Input {input.id} shared unit: {input.unit_source_text} · source
                              offsets {input.unit_source_start}–{input.unit_source_end}
                            </p>
                          ))}
                        {trace.normalized_inputs?.map((input, i) => (
                          <p key={i}>
                            Normalized {input.role}: {input.value} {input.unit} · conversion factor{' '}
                            {input.conversion_factor}
                          </p>
                        ))}
                      </details>
                    </div>
                  ))}
                </div>
              )}
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
                      tabIndex={-1}
                      className="rounded bg-muted p-3 text-sm space-y-1"
                    >
                      <strong className="break-all">
                        [{i + 1}] {evidenceText(e.citation_id, 'Evidence')} ·{' '}
                        {evidenceText(e.kind, 'source')}
                      </strong>
                      <p className="whitespace-pre-wrap">
                        {evidenceText(e.presented_content, evidenceText(e.content))}
                      </p>
                      <p className="break-all text-xs text-muted-foreground">
                        {evidenceText(e.relative_path)} {evidenceText(e.anchor)} · revision{' '}
                        {evidenceText(e.revision_id, 'see sources')} · {evidenceText(e.reviewer)}
                      </p>
                      {Array.isArray(e.sources) && (
                        <ul className="break-all text-xs">
                          {(e.sources as Array<Record<string, unknown>>).map((source, n) => (
                            <li key={n} id={`investigation-source-${i + 1}-${n + 1}`} tabIndex={-1}>
                              {evidenceText(source.citation_id)} ·{' '}
                              {evidenceText(source.relative_path, evidenceText(source.document_id))}{' '}
                              · {evidenceText(source.anchor)} · revision{' '}
                              {evidenceText(source.revision_id)}
                              <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
                                {evidenceText(
                                  source.presented_content,
                                  evidenceText(source.content),
                                )}
                              </p>
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
                    {corrected.questions ? (
                      <div className="space-y-4">
                        <p className="text-sm text-muted-foreground">
                          Choose source excerpts for each answer. The saved excerpts come from the
                          source catalog. Editing an answer does not recalculate the original trace.
                        </p>
                        {corrected.questions.map((question) => {
                          const answers = corrected.answers;
                          const answer = answers?.find(
                            (entry) => entry.question_id === question.id,
                          );
                          if (!answer || !answers) return null;
                          const updateAnswer = (changes: Partial<typeof answer>) =>
                            setCorrected({
                              ...corrected,
                              answers: answers.map((entry) => {
                                if (entry.question_id !== question.id) return entry;
                                if (changes.snippet_ids) {
                                  const withoutExcerpts = { ...entry };
                                  delete withoutExcerpts.source_excerpts;
                                  return { ...withoutExcerpts, ...changes };
                                }
                                return { ...entry, ...changes };
                              }),
                            });
                          return (
                            <div
                              key={question.id}
                              className="space-y-2 border-t border-border pt-3"
                            >
                              <h4 className="text-sm font-medium">{question.text}</h4>
                              <label className="block text-xs">
                                Answer status
                                <select
                                  className={field}
                                  disabled={busy}
                                  value={answer.status}
                                  onChange={(e) =>
                                    updateAnswer({ status: e.target.value as typeof answer.status })
                                  }
                                >
                                  <option value="supported">Supported (model claim)</option>
                                  <option value="unknown">Unknown</option>
                                  <option value="conflicting">Conflicting</option>
                                </select>
                              </label>
                              <label className="block text-xs">
                                Answer text
                                <textarea
                                  className={`${field} resize-none`}
                                  rows={3}
                                  disabled={busy}
                                  value={answer.text}
                                  onChange={(e) => updateAnswer({ text: e.target.value })}
                                />
                              </label>
                              <fieldset className="space-y-2" disabled={busy}>
                                <legend className="text-xs">Source excerpts</legend>
                                {(current.provenance?.snippet_catalog ?? []).map((snippet) => (
                                  <label
                                    key={snippet.snippet_id}
                                    className="flex items-start gap-2 text-xs"
                                  >
                                    <input
                                      type="checkbox"
                                      checked={answer.snippet_ids.includes(snippet.snippet_id)}
                                      onChange={(e) =>
                                        updateAnswer({
                                          snippet_ids: e.target.checked
                                            ? [...answer.snippet_ids, snippet.snippet_id]
                                            : answer.snippet_ids.filter(
                                                (id) => id !== snippet.snippet_id,
                                              ),
                                        })
                                      }
                                    />
                                    <span>
                                      <strong>{snippet.snippet_id}</strong> · {snippet.quote}
                                    </span>
                                  </label>
                                ))}
                              </fieldset>
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <>
                        <p className="text-sm text-muted-foreground">
                          For each fact or prior fix citation, add an exact excerpt from its source.
                          For a reviewed claim, use a source citation ID shown under its provenance.
                          Open citations above to copy source text and IDs.
                        </p>
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
                                {(item.source_excerpts ?? []).map((excerpt, n) => (
                                  <div key={n} className="space-y-1">
                                    <label className="block text-xs">
                                      Source citation ID
                                      <input
                                        className={field}
                                        value={excerpt.citation_id}
                                        onChange={(event) =>
                                          setCorrected({
                                            ...corrected,
                                            [key]: corrected[key].map((old, j) =>
                                              j === i
                                                ? {
                                                    ...old,
                                                    source_excerpts: (
                                                      old.source_excerpts ?? []
                                                    ).map((ex, k) =>
                                                      k === n
                                                        ? { ...ex, citation_id: event.target.value }
                                                        : ex,
                                                    ),
                                                  }
                                                : old,
                                            ),
                                          })
                                        }
                                      />
                                    </label>
                                    <label className="block text-xs">
                                      Exact excerpt from source
                                      <textarea
                                        className={`${field} resize-none`}
                                        rows={2}
                                        value={excerpt.quote}
                                        onChange={(event) =>
                                          setCorrected({
                                            ...corrected,
                                            [key]: corrected[key].map((old, j) =>
                                              j === i
                                                ? {
                                                    ...old,
                                                    source_excerpts: (
                                                      old.source_excerpts ?? []
                                                    ).map((ex, k) =>
                                                      k === n
                                                        ? { ...ex, quote: event.target.value }
                                                        : ex,
                                                    ),
                                                  }
                                                : old,
                                            ),
                                          })
                                        }
                                      />
                                    </label>
                                    <button
                                      className={button}
                                      onClick={() =>
                                        setCorrected({
                                          ...corrected,
                                          [key]: corrected[key].map((old, j) =>
                                            j === i
                                              ? {
                                                  ...old,
                                                  source_excerpts: (
                                                    old.source_excerpts ?? []
                                                  ).filter((_, k) => k !== n),
                                                }
                                              : old,
                                          ),
                                        })
                                      }
                                    >
                                      Remove excerpt
                                    </button>
                                  </div>
                                ))}
                                <button
                                  className={button}
                                  onClick={() =>
                                    setCorrected({
                                      ...corrected,
                                      [key]: corrected[key].map((old, j) =>
                                        j === i
                                          ? {
                                              ...old,
                                              source_excerpts: [
                                                ...(old.source_excerpts ?? []),
                                                { citation_id: '', quote: '' },
                                              ],
                                            }
                                          : old,
                                      ),
                                    })
                                  }
                                >
                                  Add source excerpt
                                </button>
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
                                  [key]: [
                                    ...corrected[key],
                                    { text: '', citation_ids: [], source_excerpts: [] },
                                  ],
                                })
                              }
                            >
                              Add {label.toLowerCase()} item
                            </button>
                          </div>
                        ))}
                        <label className="block text-sm">
                          Model questions (unverified), one per line
                          <textarea
                            className={`${field} resize-none`}
                            rows={3}
                            value={(corrected.model_uncertainties ?? []).join('\n')}
                            onChange={(e) =>
                              setCorrected({
                                ...corrected,
                                model_uncertainties: e.target.value.split('\n'),
                              })
                            }
                          />
                        </label>
                      </>
                    )}
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
                      if (decision === 'correct' && corrected?.questions && !corrected.answers) {
                        throw new Error('Answers are unavailable. Reload the investigation.');
                      }
                      const item = await localApi.reviewInvestigation(current.id, {
                        ...scope,
                        decision,
                        reviewer,
                        reason,
                        ...(decision === 'correct' && corrected
                          ? {
                              corrected_brief: {
                                ...(corrected.questions
                                  ? {
                                      questions: corrected.questions,
                                      answers: (corrected.answers ?? []).map(
                                        ({ question_id, status, text, snippet_ids }) => ({
                                          question_id,
                                          status,
                                          text,
                                          snippet_ids,
                                        }),
                                      ),
                                    }
                                  : {}),
                                facts: corrected.facts,
                                prior_fixes: corrected.prior_fixes,
                                hypotheses: corrected.hypotheses,
                                recommended_checks: corrected.recommended_checks,
                                model_uncertainties: (corrected.model_uncertainties ?? [])
                                  .map((v) => v.trim())
                                  .filter(Boolean),
                              },
                            }
                          : {}),
                      });
                      show(item);
                      setNotice('Review saved.');
                      await refresh();
                    }, true)
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
