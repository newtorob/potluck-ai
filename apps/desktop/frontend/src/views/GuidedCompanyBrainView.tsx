import { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { CompanyBrainPilotSetup } from '@/components/investigations/CompanyBrainPilotSetup';
import { FocusedSourceAnswer } from '@/components/investigations/FocusedSourceAnswer';
import {
  GuidedAnswerReview,
  type GuidedReviewInput,
} from '@/components/investigations/GuidedAnswerReview';
import {
  SuppliedEvidencePanel,
  type SuppliedEvidencePanelHandle,
} from '@/components/investigations/SuppliedEvidencePanel';
import {
  QuestionPartsEditor,
  questionPartsError,
} from '@/components/investigations/QuestionPartsEditor';
import {
  investigationButton as button,
  investigationField as field,
} from '@/components/investigations/controls';
import { localApi } from '@/lib/localApi';
import { ipc } from '@/lib/ipc';
import { suggestQuestionParts } from '@/lib/questionPartSuggestions';
import { useHealth } from '@/hooks/useHealth';
import { useCompanyBrainWorkspaceStore } from '@/stores/companyBrainWorkspaceStore';
import { useModelStore } from '@/stores/modelStore';
import type { Investigation, SourceCollection } from '@/types/investigations';
import type { PilotClaim, PilotSourceDraft } from '@/types/companyBrainPilot';

function correctedAnswerCitations(
  answer: NonNullable<Investigation['brief']['answers']>[number],
  evidence: Investigation['evidence'],
): string[] {
  if (
    answer.passage_presentation ||
    !Array.isArray(answer.snippet_ids) ||
    !Array.isArray(answer.source_excerpts)
  )
    return [];
  const selected = new Set(
    answer.snippet_ids.filter((id): id is string => typeof id === 'string' && !!id.trim()),
  );
  const citations = new Set<string>();
  for (const excerpt of answer.source_excerpts) {
    if (
      !excerpt ||
      typeof excerpt.snippet_id !== 'string' ||
      !selected.has(excerpt.snippet_id) ||
      typeof excerpt.citation_id !== 'string' ||
      !excerpt.citation_id.trim() ||
      typeof excerpt.quote !== 'string' ||
      !excerpt.quote.trim()
    )
      continue;
    const matches = evidence.filter((row) => row.citation_id === excerpt.citation_id);
    if (
      matches.length !== 1 ||
      matches[0]?.kind !== 'reviewed_claim' ||
      typeof matches[0].content !== 'string' ||
      !matches[0].content.includes(excerpt.quote)
    )
      continue;
    citations.add(excerpt.citation_id);
  }
  return [...citations];
}

function sameSource(claim: PilotClaim, draft: PilotSourceDraft): boolean {
  const preview = draft.preview;
  return (
    !!preview &&
    claim.content === preview.content &&
    JSON.stringify([...new Set(claim.sources.map((s) => s.citation_id))].sort()) ===
      JSON.stringify([...preview.citation_ids].sort())
  );
}
function priorSource(claims: PilotClaim[], draft: PilotSourceDraft): PilotClaim | undefined {
  const related = claims.filter((c) => c.sources.some((s) => s.document_id === draft.document_id));
  if (
    related.length > 1 ||
    related.some((c) => c.sources.some((s) => s.document_id !== draft.document_id))
  )
    throw new Error('Multiple reviewed statements need advanced review');
  return related[0];
}
function failure(error: unknown): string {
  const message = error instanceof Error ? error.message : '';
  if (/Multiple reviewed/.test(message))
    return 'Several reviewed statements depend on this document. Open Advanced investigations to review their replacement together.';
  if (/404|unavailable|stale/i.test(message))
    return 'This source or answer is no longer current. Scan your folders and review the changed documents, then try again.';
  if (/collections_unavailable|ready and recent/i.test(message))
    return 'Scan every selected folder successfully before continuing.';
  if (/model|profile/i.test(message))
    return 'Check Local model setup, then refresh this workspace. This flow requires the supported Qwen3.8 27B native profile.';
  return 'Could not complete this action. Your question is kept. Open Workspace and folders, check its sources, then try again.';
}

export function GuidedCompanyBrainView({
  onAdvanced,
  initialQuestion = '',
  reviewedEvidenceVersion = 'v5',
  reviewedAnswerVersion = 'v32',
}: {
  onAdvanced: (question: string, answerId?: string) => void;
  initialQuestion?: string;
  reviewedEvidenceVersion?: 'v5' | 'v6';
  reviewedAnswerVersion?: 'v32' | 'v33' | 'v34' | 'v35' | 'v36' | 'v37' | 'v38';
}) {
  const health = useHealth();
  const saved = useCompanyBrainWorkspaceStore((s) => s.saved);
  const activeModel = useModelStore((s) => s.activeModelSlug);
  const fetchModels = useModelStore((s) => s.fetchModels);
  const [workspace, setWorkspace] = useState(saved?.workspace ?? 'Company Brain');
  const [loaded, setLoaded] = useState('');
  const [ids, setIds] = useState<string[]>([]);
  const [collections, setCollections] = useState<SourceCollection[]>([]);
  const [model, setModel] = useState(saved?.modelSlug || activeModel || '');
  const [folderName, setFolderName] = useState('');
  const [folder, setFolder] = useState('');
  const [consent, setConsent] = useState(false);
  const [question, setQuestion] = useState(initialQuestion);
  const [parts, setParts] = useState<string[]>([]);
  const [confirmation, setConfirmation] = useState('');
  const [drafts, setDrafts] = useState<PilotSourceDraft[]>([]);
  const [suggestionNotes, setSuggestionNotes] = useState<string[]>([]);
  const [suggestionIncomplete, setSuggestionIncomplete] = useState(false);
  const [approved, setApproved] = useState<PilotClaim[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [prepared, setPrepared] = useState(false);
  const [continueReviewed, setContinueReviewed] = useState(false);
  const [reviewer, setReviewer] = useState('Local reviewer');
  const [reason, setReason] = useState(
    'Reviewed the complete indexed source, including conditions and exceptions.',
  );
  const [answer, setAnswer] = useState<Investigation | null>(null);
  const [recent, setRecent] = useState<Investigation[]>([]);
  const [recentLoading, setRecentLoading] = useState(false);
  const [recentError, setRecentError] = useState('');
  const [operationBusy, setBusy] = useState('');
  const [setupBusy, setSetupBusy] = useState(false);
  const setupLock = useRef(false);
  const busy = operationBusy || (setupBusy ? 'Updating local model setup…' : '');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [unavailableReview, setUnavailableReview] = useState('');
  const reviewId = useId();
  const alive = useRef(true);
  const epoch = useRef(0);
  const lock = useRef(false);
  const touched = useRef(false);
  const partsEdited = useRef(false);
  const recentTicket = useRef(0);
  const confirmSection = useRef<HTMLElement>(null);
  const sourceCard = useRef<HTMLElement>(null);
  const answerSection = useRef<HTMLElement>(null);
  const suppliedEvidence = useRef<SuppliedEvidencePanelHandle>(null);
  const workspaceDetails = useRef<HTMLDetailsElement>(null);
  const scope = { workspace: loaded, collection_ids: ids };
  const scopeKey = JSON.stringify([loaded, [...ids].sort()]);
  const answerContext = useRef({ scopeKey, id: answer?.id });
  answerContext.current = { scopeKey, id: answer?.id };
  const effectiveBrief = answer?.review?.corrected_brief ?? answer?.brief;
  const reviewKey = answer ? `${scopeKey}:${answer.id}` : '';
  const ready =
    loaded === workspace.trim() &&
    ids.length > 0 &&
    ids.every((id) =>
      collections.some((c) => {
        if (c.id !== id || c.scan_status !== 'ready' || !c.last_scan_at) return false;
        // Match the source snapshot's 24-hour bound; the server rechecks freshness.
        const age = Date.now() - Date.parse(c.last_scan_at);
        return age >= 0 && age <= 24 * 60 * 60 * 1000;
      }),
    );
  const connected = health.hasChecked && health.sidecarUp;
  const selectedModelLoaded =
    connected &&
    health.modelLoaded &&
    !!model &&
    model === activeModel &&
    model === health.activeModelId;
  const binding = JSON.stringify([
    scopeKey,
    question,
    parts,
    model,
    activeModel,
    reviewedEvidenceVersion,
    reviewedAnswerVersion,
    collections.map((c) => [c.id, c.scan_status, c.last_scan_at]),
    approved.map((c) => [c.id, c.sources.map((s) => [s.citation_id, s.revision_id])]),
  ]);
  const canAsk =
    ready &&
    prepared &&
    continueReviewed &&
    approved.length > 0 &&
    !truncated &&
    !!model &&
    model === activeModel &&
    selectedModelLoaded &&
    !questionPartsError(parts) &&
    confirmation === binding;
  function current(ticket: number) {
    return alive.current && epoch.current === ticket;
  }
  function showWorkspace() {
    if (!workspaceDetails.current) return;
    workspaceDetails.current.open = true;
    workspaceDetails.current.querySelector('summary')?.focus();
  }
  function protect() {
    if (touched.current) return;
    touched.current = true;
    useCompanyBrainWorkspaceStore.setState((s) => ({ revision: s.revision + 1 }));
  }
  function clearDraft(keepParts = false) {
    setConfirmation('');
    setDrafts([]);
    setSuggestionNotes([]);
    setSuggestionIncomplete(false);
    setPrepared(false);
    setContinueReviewed(false);
    if (!keepParts) {
      setParts([]);
      partsEdited.current = false;
    }
    setAnswer(null);
  }
  function remember(name: string, selected: string[], slug = model) {
    useCompanyBrainWorkspaceStore
      .getState()
      .saveWorkspace({ version: 1, workspace: name, collectionIds: selected, modelSlug: slug });
  }
  async function claims() {
    const [a, p] = await Promise.all([
      localApi.pilotClaims({ ...scope, status: 'approved' }),
      localApi.pilotClaims({ ...scope, status: 'proposed' }),
    ]);
    return { approved: a.claims, proposed: p.claims, truncated: a.truncated || p.truncated };
  }
  function applyClaims(data: Awaited<ReturnType<typeof claims>>) {
    setApproved(data.approved);
    setTruncated(data.truncated);
  }
  async function run(label: string, action: (ticket: number) => Promise<void>) {
    if (lock.current || setupLock.current) return;
    lock.current = true;
    const ticket = epoch.current;
    setBusy(label);
    setError('');
    setNotice('');
    try {
      await action(ticket);
    } catch (e) {
      if (current(ticket)) {
        clearDraft(true);
        setApproved([]);
        setError(failure(e));
        // A timed-out write may have committed; refresh inventory without repeating it.
        if (loaded) {
          try {
            const data = await localApi.sourceCollections(loaded);
            if (current(ticket)) setCollections(data.collections);
          } catch {
            /* Keep the original recovery guidance. */
          }
        }
      }
    } finally {
      if (current(ticket)) {
        lock.current = false;
        setBusy('');
      }
    }
  }
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      epoch.current++;
    };
  }, []);
  useEffect(() => {
    setConfirmation('');
  }, [health.hasChecked, health.sidecarUp, health.modelLoaded, health.activeModelId]);
  useEffect(() => {
    setConfirmation('');
    setAnswer(null);
  }, [reviewedEvidenceVersion, reviewedAnswerVersion]);
  useEffect(() => {
    if (!saved || touched.current) return;
    const ticket = ++epoch.current;
    lock.current = true;
    setBusy('Reopening workspace…');
    setError('');
    setWorkspace(saved.workspace);
    setLoaded('');
    setIds([]);
    setModel(saved.modelSlug || activeModel || '');
    void localApi
      .sourceCollections(saved.workspace)
      .then(async (data) => {
        if (!current(ticket)) return;
        setCollections(data.collections);
        setLoaded(saved.workspace);
        setIds(saved.collectionIds.filter((id) => data.collections.some((c) => c.id === id)));
        await fetchModels();
      })
      .catch((e) => {
        if (current(ticket)) setError(failure(e));
      })
      .finally(() => {
        if (current(ticket)) {
          lock.current = false;
          setBusy('');
        }
      });
  }, [saved, fetchModels]);
  useEffect(() => {
    const ticket = ++recentTicket.current;
    setRecent([]);
    setRecentError('');
    if (!loaded || !ids.length) {
      setRecentLoading(false);
      return;
    }
    setRecentLoading(true);
    void localApi
      .investigations(scope)
      .then((data) => {
        if (alive.current && ticket === recentTicket.current) setRecent(data.investigations);
      })
      .catch(() => {
        if (alive.current && ticket === recentTicket.current)
          setRecentError('Could not load saved questions. Refresh this workspace to retry.');
      })
      .finally(() => {
        if (alive.current && ticket === recentTicket.current) setRecentLoading(false);
      });
  }, [scopeKey]); // Exact scope owns the recent list; source validity is rechecked on open.

  async function openWorkspace() {
    protect();
    clearDraft(true);
    recentTicket.current++;
    setRecentLoading(false);
    setRecent([]);
    setApproved([]);
    const name = workspace.trim();
    if (!name) {
      setError('Enter a workspace name.');
      return;
    }
    await run('Opening workspace…', async (ticket) => {
      const data = await localApi.sourceCollections(name);
      await fetchModels();
      if (!current(ticket)) return;
      const selected =
        saved?.workspace === name
          ? saved.collectionIds.filter((id) => data.collections.some((c) => c.id === id))
          : [];
      if (
        name !== loaded ||
        JSON.stringify([...selected].sort()) !== JSON.stringify([...ids].sort())
      )
        clearDraft();
      setLoaded(name);
      setCollections(data.collections);
      setIds(selected);
      remember(name, selected);
      setNotice('Workspace opened. Choose the folders this question may use.');
    });
  }
  async function prepare() {
    clearDraft(true);
    await run('Finding source drafts…', async (ticket) => {
      const [draft, knowledge] = await Promise.all([
        localApi.pilotSetupDrafts({
          ...scope,
          question: question.trim(),
          suggestion_version: 'document-topics-v4',
        }),
        claims(),
      ]);
      if (!current(ticket)) return;
      applyClaims(knowledge);
      setDrafts(draft.candidates);
      const omitted = draft.query_trace?.omitted_terms ?? [];
      setSuggestionNotes([
        ...draft.limitations,
        ...(omitted.length
          ? [`Some question words were not used for ranking: ${omitted.join(', ')}.`]
          : []),
      ]);
      setSuggestionIncomplete(draft.truncated || omitted.length > 0);
      if (!partsEdited.current) setParts(suggestQuestionParts(question).parts);
      setPrepared(true);
      setNotice('Review the suggested documents below. No source was saved or approved.');
    });
  }
  async function approve(draft: PilotSourceDraft) {
    if (!draft.preview || !reviewer.trim() || !reason.trim()) return;
    setConfirmation('');
    setAnswer(null);
    recentTicket.current++;
    setRecentLoading(false);
    setRecent([]);
    await run('Saving your source review…', async (ticket) => {
      const latest = await claims();
      if (!current(ticket)) return;
      if (latest.truncated) throw new Error('Source inventory unavailable');
      if (latest.approved.some((c) => sameSource(c, draft))) {
        applyClaims(latest);
        setContinueReviewed(true);
        return;
      }
      const old = priorSource(latest.approved, draft);
      const key = old?.claim_key ?? `document:${draft.document_id}`;
      let replacement = latest.proposed.find(
        (c) => c.claim_key === key && sameSource(c, draft),
      )?.id;
      if (!replacement)
        replacement = (
          await localApi.proposePilotClaim({
            ...scope,
            claim_key: key,
            content: draft.preview!.content,
            citation_ids: draft.preview!.citation_ids,
          })
        ).claim_id;
      if (!current(ticket)) return;
      if (old)
        await localApi.supersedePilotClaim(old.id, {
          ...scope,
          replacement_id: replacement,
          reviewer: reviewer.trim(),
          reason: reason.trim(),
        });
      else
        await localApi.approvePilotClaim(replacement, {
          ...scope,
          reviewer: reviewer.trim(),
          reason: reason.trim(),
        });
      const [knowledge, history] = await Promise.all([claims(), localApi.investigations(scope)]);
      if (!current(ticket)) return;
      applyClaims(knowledge);
      setRecent(history.investigations);
      setContinueReviewed(true);
      setNotice(
        prepared
          ? 'Source review saved. Confirm what the question should cover.'
          : 'Source review saved. Enter a question, then choose Review sources for this question.',
      );
      requestAnimationFrame(() => confirmSection.current?.focus());
    });
  }
  async function scan(collection: SourceCollection) {
    clearDraft(true);
    recentTicket.current++;
    setRecentLoading(false);
    setRecent([]);
    setApproved([]);
    await run('Scanning documents…', async (ticket) => {
      await localApi.scanPilotCollection(collection.id, {
        workspace: loaded,
        scan_confirmed: true,
      });
      const [data, history] = await Promise.all([
        localApi.sourceCollections(loaded),
        localApi.investigations(scope),
      ]);
      if (current(ticket)) {
        setCollections(data.collections);
        setRecent(history.investigations);
        setNotice('Scan complete. Review changed documents below before asking again.');
      }
    });
  }
  async function changedDocument(collectionId: string, path: string) {
    setConfirmation('');
    setAnswer(null);
    setContinueReviewed(false);
    setSuggestionNotes([]);
    setSuggestionIncomplete(false);
    await run('Opening changed source…', async (ticket) => {
      const [draft, knowledge] = await Promise.all([
        localApi.pilotSourceDraft({ ...scope, collection_id: collectionId, relative_path: path }),
        claims(),
      ]);
      if (current(ticket)) {
        applyClaims(knowledge);
        setDrafts([draft]);
        requestAnimationFrame(() => sourceCard.current?.focus());
        setNotice('Review the complete current document below. Your question is unchanged.');
      }
    });
  }
  async function ask() {
    if (!canAsk) return;
    setConfirmation('');
    setAnswer(null);
    await run('Preparing your answer…', async (ticket) => {
      const result = await localApi.askReviewedPilot({
        ...scope,
        question: question.trim(),
        model_slug: model,
        pilot_confirmed: true,
        question_parts: parts,
        question_parts_confirmed: true,
        ...(reviewedEvidenceVersion === 'v6' ? { reviewed_evidence_version: 'v6' as const } : {}),
        ...(reviewedAnswerVersion !== 'v32'
          ? { reviewed_answer_version: reviewedAnswerVersion }
          : {}),
      });
      if (!current(ticket)) return;
      setAnswer(result);
      setRecent((previous) => [result, ...previous.filter((i) => i.id !== result.id)]);
      requestAnimationFrame(() => answerSection.current?.focus());
    });
  }
  function updateSavedAnswer(item: Investigation) {
    setAnswer(item);
    setUnavailableReview('');
    setRecent((previous) => [item, ...previous.filter((row) => row.id !== item.id)]);
  }
  function unavailable(error: unknown) {
    return (
      error instanceof Error && /\b(403|404)\b|unavailable|stale|freshness/i.test(error.message)
    );
  }
  async function savedAnswerAction<T>(
    action: (target: Investigation, selected: typeof scope) => Promise<T>,
    reconcile = false,
    onSuccess?: (result: T) => void,
  ): Promise<T | undefined> {
    if (!answer || lock.current || unavailableReview === reviewKey)
      throw new Error('This answer is not available for review. Reopen it from Recent questions.');
    const target = structuredClone(answer);
    const selected = { workspace: loaded, collection_ids: [...ids] };
    const key = scopeKey;
    const ticket = epoch.current;
    const active = () =>
      current(ticket) &&
      answerContext.current.scopeKey === key &&
      answerContext.current.id === target.id;
    if (
      target.workspace !== selected.workspace ||
      JSON.stringify([...target.collection_ids].sort()) !==
        JSON.stringify([...selected.collection_ids].sort())
    )
      throw new Error('The selected folders changed. Reopen the answer.');
    lock.current = true;
    setBusy(reconcile ? 'Saving your answer review…' : 'Preparing Markdown…');
    setNotice('');
    try {
      const result = await action(target, selected);
      if (!active())
        throw new Error(
          'The answer or workspace changed. Reopen the saved answer to check its state.',
        );
      onSuccess?.(result);
      return result;
    } catch (error) {
      if (reconcile && active()) {
        try {
          const savedRecord = await localApi.investigation(target.id, selected);
          if (active() && savedRecord.review && savedRecord.status !== 'draft') {
            updateSavedAnswer(savedRecord);
            setNotice('A review is saved. Showing the confirmed record.');
            return undefined;
          }
        } catch (readError) {
          if (active() && unavailable(readError)) setUnavailableReview(`${key}:${target.id}`);
        }
      }
      if (active() && unavailable(error)) setUnavailableReview(`${key}:${target.id}`);
      throw new Error(
        'Could not complete this action. Your entries are kept. Check the connection and source folders, then reopen the saved answer if needed.',
      );
    } finally {
      if (current(ticket)) {
        lock.current = false;
        setBusy('');
      }
    }
  }
  async function reviewSavedAnswer(input: GuidedReviewInput) {
    await savedAnswerAction(
      (target, selected) =>
        localApi.reviewInvestigation(target.id, { ...selected, ...structuredClone(input) }),
      true,
      (result) => {
        updateSavedAnswer(result);
        setNotice('Answer review saved. You can export the reviewed handoff below.');
      },
    );
  }
  async function exportSavedAnswer() {
    const result = await savedAnswerAction((target, selected) =>
      localApi.exportInvestigation(target.id, selected),
    );
    if (!result) throw new Error('Reopen the saved answer before exporting.');
    return result.markdown;
  }
  const sourceLink = (citation: string) => (
    <a
      className="underline underline-offset-4 hover:text-primary focus-visible:ring-2 focus-visible:ring-ring"
      href="#guided-answer-sources"
      onClick={(event) => {
        event.preventDefault();
        suppliedEvidence.current?.openCitation(citation);
      }}
    >
      {answer?.evidence.find((e) => e.citation_id === citation)?.kind === 'reviewed_claim'
        ? 'Reviewed source'
        : 'Source evidence'}
    </a>
  );
  return (
    <div
      className="flex h-full min-h-0 flex-col"
      onPointerDownCapture={protect}
      onKeyDownCapture={protect}
      onInputCapture={protect}
    >
      <ViewHeader
        title="Company Brain"
        description="Review your sources, ask a question, and inspect the evidence behind the answer."
        actions={
          <button
            className={button}
            disabled={!!busy}
            onClick={() => onAdvanced(question, answer?.id)}
          >
            Advanced investigations
          </button>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-6" aria-busy={!!busy}>
        <div className="mx-auto max-w-4xl space-y-5">
          {error && (
            <p
              role="alert"
              className="rounded-md border border-destructive p-3 text-sm text-destructive"
            >
              {error}
            </p>
          )}
          <div className="min-h-6 text-sm" role="status">
            {busy || notice}
          </div>
          <section
            className="space-y-3 rounded-lg border border-border bg-card p-4"
            aria-label="Workspace readiness"
          >
            <h2 className="font-serif text-xl">
              {!health.hasChecked
                ? 'Checking local engine…'
                : !connected
                  ? 'Local engine unavailable'
                  : ready
                    ? 'Ready to review sources'
                    : 'Finish workspace setup'}
            </h2>
            <dl className="space-y-2 text-sm">
              <div>
                <dt className="inline font-medium">Engine: </dt>
                <dd className="inline">
                  {!health.hasChecked
                    ? 'Checking connection.'
                    : connected
                      ? 'Connected.'
                      : 'Not connected. Start or reopen Potluck, then check the connection.'}
                </dd>
              </div>
              <div>
                <dt className="inline font-medium">Folders: </dt>
                <dd className="inline">
                  {!loaded
                    ? 'Open a workspace to see its folders.'
                    : !ids.length
                      ? 'Add or select a document folder.'
                      : !ready
                        ? 'Scan every selected folder successfully. Scans must be less than 24 hours old.'
                        : `${ids.length} selected; recent scans available.`}
                </dd>
              </div>
              <div>
                <dt className="inline font-medium">Model: </dt>
                <dd className="inline">
                  {!connected
                    ? 'Waiting for the engine check.'
                    : health.modelRestore?.status === 'loading'
                      ? `Restoring ${health.modelRestore.model_id ?? 'the saved model'}…`
                      : health.modelRestore?.status === 'failed' && !health.modelLoaded
                        ? 'Startup restore failed. Check Local model setup below or open Models.'
                        : !health.modelLoaded
                          ? 'No model loaded. Review sources now, or use Local model setup below before asking.'
                          : selectedModelLoaded
                            ? `Loaded: ${health.activeModelId}.`
                            : `Loaded: ${health.activeModelId ?? 'unknown'}. Choose this model in Workspace and folders, or refresh the workspace to update the choices.`}
                </dd>
              </div>
            </dl>
            <div className="flex flex-wrap gap-2">
              {!connected && (
                <button
                  className={button}
                  disabled={!!busy || !health.hasChecked}
                  onClick={health.refresh}
                >
                  Check connection
                </button>
              )}
              <button className={button} disabled={!!busy} onClick={showWorkspace}>
                {ready ? 'Edit workspace and folders' : 'Set up workspace and folders'}
              </button>
              <Link to="/models" className={button}>
                Open Models
              </Link>
            </div>
            <p className="text-xs text-muted-foreground">
              Source review does not need a model. Answers require the supported Qwen3.8 27B native
              profile, checked again when asking. Nothing is loaded, scanned or approved
              automatically here.
            </p>
            <CompanyBrainPilotSetup
              disabled={!!operationBusy || !connected}
              onBusyChange={(value) => {
                setupLock.current = value;
                if (alive.current) setSetupBusy(value);
              }}
              onModelChange={(id) => {
                if (!alive.current) return;
                setConfirmation('');
                if (id && id !== model) {
                  setModel(id);
                  setAnswer(null);
                  if (loaded) remember(loaded, ids, id);
                }
                health.refresh();
                void fetchModels().catch(() => {
                  if (alive.current)
                    setNotice(
                      'Model setup changed. Refresh the workspace to update its model choices.',
                    );
                });
              }}
            />
          </section>
          <details
            ref={workspaceDetails}
            open={!loaded || !collections.length}
            className="rounded-lg border border-border bg-card p-4"
          >
            <summary className="cursor-pointer font-medium hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
              Workspace and folders{loaded ? ` · ${loaded} · ${ids.length} selected` : ''}
            </summary>
            <fieldset disabled={!!busy} className="mt-4 space-y-3">
              <div className="flex flex-wrap items-end gap-2">
                <label className="min-w-0 flex-1 text-sm">
                  Workspace name
                  <input
                    className={field}
                    maxLength={256}
                    value={workspace}
                    onChange={(e) => {
                      protect();
                      epoch.current++;
                      setWorkspace(e.target.value);
                      setConsent(false);
                      setLoaded('');
                      setIds([]);
                      clearDraft();
                    }}
                  />
                </label>
                <button className={button} onClick={() => void openWorkspace()}>
                  {loaded ? 'Refresh workspace' : 'Open workspace'}
                </button>
              </div>
              {loaded && (
                <>
                  <fieldset className="space-y-2">
                    <legend className="text-sm font-medium">Folders available to answers</legend>
                    {collections.length ? (
                      collections.map((c) => (
                        <label className="flex items-start gap-2 text-sm" key={c.id}>
                          <input
                            type="checkbox"
                            checked={ids.includes(c.id)}
                            onChange={(e) => {
                              epoch.current++;
                              clearDraft();
                              setApproved([]);
                              const next = e.target.checked
                                ? [...ids, c.id]
                                : ids.filter((id) => id !== c.id);
                              setIds(next);
                              remember(loaded, next);
                            }}
                          />
                          {c.name} · {c.scan_status}
                        </label>
                      ))
                    ) : (
                      <p className="text-sm text-muted-foreground">
                        Add a Markdown folder to get started.
                      </p>
                    )}
                  </fieldset>
                  <details>
                    <summary className="cursor-pointer text-sm hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
                      Add a document folder
                    </summary>
                    <div className="mt-3 space-y-3">
                      <button
                        className={button}
                        onClick={() =>
                          void run('Choosing folder…', async (ticket) => {
                            const path = await ipc.selectDirectory();
                            if (current(ticket) && path) {
                              setFolder(path);
                              setConsent(false);
                              if (!folderName.trim())
                                setFolderName(
                                  path.split(/[/\\]/).filter(Boolean).at(-1) ?? 'Documents',
                                );
                            }
                          })
                        }
                      >
                        Choose folder
                      </button>
                      <label className="block text-sm">
                        Folder name
                        <input
                          className={field}
                          value={folderName}
                          maxLength={256}
                          onChange={(e) => setFolderName(e.target.value)}
                        />
                      </label>
                      <label className="block text-sm">
                        Full folder path
                        <input
                          className={field}
                          value={folder}
                          onChange={(e) => {
                            setFolder(e.target.value);
                            setConsent(false);
                          }}
                        />
                      </label>
                      <p className="text-xs text-muted-foreground">
                        Scans read Markdown files on this machine. Nothing is uploaded or approved.
                      </p>
                      <label className="flex items-start gap-2 text-sm">
                        <input
                          type="checkbox"
                          checked={consent}
                          onChange={(e) => setConsent(e.target.checked)}
                        />
                        Allow Potluck to read this folder for this workspace.
                      </label>
                      <button
                        className={button}
                        disabled={!consent || !folder.trim() || !folderName.trim()}
                        onClick={() => {
                          clearDraft();
                          void run('Adding and scanning folder…', async (ticket) => {
                            const result = await localApi.registerPilotCollection({
                              workspace: loaded,
                              name: folderName.trim(),
                              folder: folder.trim(),
                              scan_confirmed: true,
                            });
                            await localApi.scanPilotCollection(result.collection_id, {
                              workspace: loaded,
                              scan_confirmed: true,
                            });
                            const data = await localApi.sourceCollections(loaded);
                            if (current(ticket)) {
                              const next = [...new Set([...ids, result.collection_id])];
                              setCollections(data.collections);
                              setIds(next);
                              remember(loaded, next);
                              setConsent(false);
                              setFolder('');
                              setFolderName('');
                              setNotice(
                                'Folder added. Enter a question to review matching documents.',
                              );
                            }
                          });
                        }}
                      >
                        Add and scan folder
                      </button>
                    </div>
                  </details>
                  <label className="block text-sm">
                    Loaded local model
                    <select
                      className={field}
                      value={model}
                      onChange={(e) => {
                        setModel(e.target.value);
                        setAnswer(null);
                        setConfirmation('');
                        remember(loaded, ids, e.target.value);
                      }}
                    >
                      <option value="">Choose the loaded model</option>
                      {model && model !== activeModel && (
                        <option value={model}>{model} (not loaded)</option>
                      )}
                      {activeModel && <option value={activeModel}>{activeModel}</option>}
                    </select>
                  </label>
                  <p className="text-xs text-muted-foreground">
                    Uses the supported Qwen3.8 27B native profile.{' '}
                    <Link
                      to="/models"
                      className="underline hover:text-primary focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      Open Models
                    </Link>{' '}
                    manages other models. Use Local model setup above to import and explicitly
                    prepare the supplied pilot model for this session.
                  </p>
                </>
              )}
            </fieldset>
          </details>
          {!!collections.length && (
            <section
              className="space-y-3 rounded-lg border border-border bg-card p-4"
              aria-label="Document changes"
            >
              <h2 className="font-serif text-xl">Document changes</h2>
              <p className="text-xs text-muted-foreground">
                Scan to check current files. Changed documents lose approval and clear saved answers
                using their collection; unchanged approvals remain.
              </p>
              {collections
                .filter((c) => ids.includes(c.id))
                .map((c) => (
                  <div key={c.id} className="space-y-2 border-t border-border pt-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="text-sm">
                        {c.name} · {c.scan_status}
                      </p>
                      <button className={button} disabled={!!busy} onClick={() => void scan(c)}>
                        Scan {c.name}
                      </button>
                    </div>
                    {c.last_scan_report && (
                      <>
                        {c.scan_status !== 'ready' && (
                          <p className="text-sm">
                            This folder needs a successful scan. The report below is from its last
                            successful scan.
                          </p>
                        )}
                        <p className="text-xs text-muted-foreground">
                          Last successful scan: {c.last_scan_report.files_updated} updated or
                          reindexed, {c.last_scan_report.files_added} added,{' '}
                          {c.last_scan_report.files_removed} removed. Approved statements revoked:{' '}
                          {c.last_scan_report.revoked_claims}. Saved answers cleared:{' '}
                          {c.last_scan_report.invalidated_answers}.
                        </p>
                        {c.last_scan_report.changes.length > 0 ? (
                          <details>
                            <summary className="cursor-pointer text-sm hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
                              Changed documents ({c.last_scan_report.changes.length})
                            </summary>
                            <div className="mt-3 max-h-80 space-y-3 overflow-y-auto">
                              {c.last_scan_report.changes.map((change, index) => (
                                <div
                                  key={index}
                                  className="flex flex-wrap items-center justify-between gap-2 text-sm"
                                >
                                  <span className="break-all">
                                    {change.change}: {change.relative_path}
                                  </span>
                                  {change.change !== 'removed' && (
                                    <button
                                      className={button}
                                      disabled={!!busy || !ready}
                                      onClick={() =>
                                        void changedDocument(c.id, change.relative_path)
                                      }
                                    >
                                      Review {change.relative_path}
                                    </button>
                                  )}
                                </div>
                              ))}
                            </div>
                          </details>
                        ) : (
                          <p className="text-sm text-muted-foreground">
                            No document changes in the last successful scan.
                          </p>
                        )}
                        {c.last_scan_report.changes_truncated && (
                          <p className="text-xs text-muted-foreground">
                            Showing the first 100 changed paths. Use Advanced investigations for the
                            full collection.
                          </p>
                        )}
                      </>
                    )}
                  </div>
                ))}
            </section>
          )}
          <section
            className="space-y-4 rounded-lg border border-border bg-card p-5"
            aria-label="Review sources"
          >
            <h2 className="font-serif text-xl">1. Review sources for your question</h2>
            <label className="block text-sm">
              Your question
              <textarea
                className={`${field} resize-none`}
                rows={3}
                maxLength={500}
                disabled={!!busy}
                value={question}
                onChange={(e) => {
                  epoch.current++;
                  setQuestion(e.target.value);
                  clearDraft();
                }}
              />
            </label>
            {!ready && (
              <p className="text-sm text-muted-foreground">
                Open a workspace, then add or select and scan its folders above.
              </p>
            )}
            <button
              className={button}
              disabled={!!busy || !connected || !ready || !question.trim()}
              onClick={() => void prepare()}
            >
              Review sources for this question
            </button>
            <p className="text-xs text-muted-foreground">
              Suggestions help you review new documents. Answers may use all current approved
              statements in your selected folders.
            </p>
            {prepared && !drafts.length && (
              <p className="text-sm">
                No strong word matches for new source drafts. Try a specific topic or document name,
                continue with current reviewed knowledge, or use Advanced investigations.
              </p>
            )}
            {suggestionIncomplete && (
              <p role="status" className="text-sm text-muted-foreground">
                More matching documents or indexed text may be available. Refine the question or
                review sources in Advanced investigations.
              </p>
            )}
            {!!suggestionNotes.length && (
              <details>
                <summary className="cursor-pointer text-sm hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
                  How documents were suggested
                </summary>
                {suggestionNotes.map((note, i) => (
                  <p key={i} className="mt-2 text-xs text-muted-foreground">
                    {note}
                  </p>
                ))}
              </details>
            )}
            {drafts.map((draft, index) => {
              const reviewed = approved.some((c) => sameSource(c, draft));
              let old: PilotClaim | undefined;
              let ambiguous = false;
              try {
                old = priorSource(approved, draft);
              } catch {
                ambiguous = true;
              }
              return (
                <article
                  key={`${draft.document_id}:${draft.revision_id}`}
                  ref={index === 0 ? sourceCard : undefined}
                  tabIndex={-1}
                  className="space-y-3 rounded-md border border-border p-4"
                  aria-label={`Source ${draft.relative_path}`}
                >
                  <h3 className="break-words font-medium">{draft.relative_path}</h3>
                  {draft.match && (
                    <p className="text-xs text-muted-foreground">
                      Normalized word matches:{' '}
                      {draft.match.title_terms.length > 0 &&
                        `title/headings: ${draft.match.title_terms.join(', ')}. `}
                      {draft.match.text_terms.length > 0 &&
                        `indexed text: ${draft.match.text_terms.join(', ')}.`}
                    </p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    {reviewed
                      ? 'Current reviewed document. No new approval is needed.'
                      : 'Not approved. Review the complete text and its conditions before allowing it to inform answers.'}
                  </p>
                  {draft.preview ? (
                    <>
                      <details open={!reviewed}>
                        <summary className="cursor-pointer text-sm hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
                          Complete indexed source and citations
                        </summary>
                        <pre
                          tabIndex={0}
                          className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 font-sans text-sm focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          {draft.preview.content}
                        </pre>
                        <p className="mt-2 break-all text-xs text-muted-foreground">
                          Citations: {draft.preview.citation_ids.join(', ')}
                        </p>
                        {draft.preview.limitations.map((s, i) => (
                          <p key={i} className="text-xs text-muted-foreground">
                            {s}
                          </p>
                        ))}
                      </details>
                      {ambiguous && (
                        <p className="text-sm text-muted-foreground">
                          Several reviewed statements reference this document. Review their
                          replacement in Advanced investigations.
                        </p>
                      )}
                      {!reviewed && old && (
                        <details>
                          <summary className="cursor-pointer text-sm hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
                            Previously approved statement to replace
                          </summary>
                          <pre className="mt-2 whitespace-pre-wrap break-words text-sm">
                            {old.content}
                          </pre>
                        </details>
                      )}
                      <button
                        className={button}
                        disabled={
                          !!busy ||
                          reviewed ||
                          ambiguous ||
                          truncated ||
                          !reviewer.trim() ||
                          !reason.trim()
                        }
                        onClick={() => void approve(draft)}
                      >
                        {reviewed
                          ? 'Already reviewed'
                          : old
                            ? 'Approve and replace previous statement'
                            : 'Approve document for answers'}
                      </button>
                    </>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      {draft.notice ||
                        'A complete draft is unavailable. Review excerpts in Advanced investigations.'}
                    </p>
                  )}
                </article>
              );
            })}
            {!!drafts.length && (
              <details>
                <summary className="cursor-pointer text-sm hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
                  Review details
                </summary>
                <label className="mt-2 block text-sm">
                  Reviewer label
                  <input
                    className={field}
                    disabled={!!busy}
                    value={reviewer}
                    onChange={(e) => setReviewer(e.target.value)}
                  />
                </label>
                <label className="block text-sm">
                  Review note
                  <textarea
                    className={`${field} resize-none`}
                    rows={2}
                    maxLength={4000}
                    disabled={!!busy}
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </label>
                <p className="text-xs text-muted-foreground">
                  This records a local review decision, not an authenticated company role.
                </p>
              </details>
            )}
            {truncated && (
              <p role="alert" className="text-sm text-destructive">
                The knowledge list is incomplete. Use Advanced investigations to review this scope
                before continuing.
              </p>
            )}
            {prepared && (
              <button
                className={button}
                disabled={!!busy || !approved.length || truncated}
                onClick={() => {
                  setContinueReviewed(true);
                  requestAnimationFrame(() => confirmSection.current?.focus());
                }}
              >
                Continue with current reviewed knowledge ({approved.length})
              </button>
            )}
          </section>
          {prepared && continueReviewed && (
            <section
              ref={confirmSection}
              tabIndex={-1}
              className="space-y-4 rounded-lg border border-border bg-card p-5 focus-visible:ring-2 focus-visible:ring-ring"
              aria-label="Confirm question"
            >
              <h2 className="font-serif text-xl">2. Confirm the question</h2>
              <p className="text-sm text-muted-foreground">
                Answers use current approved statements from your selected folders ({ids.length}).
                Review all requested parts and shared conditions.
              </p>
              <QuestionPartsEditor
                reviewFirst
                note={
                  partsEdited.current
                    ? 'These parts include your changes. Compare them with the original question before confirming.'
                    : suggestQuestionParts(question).note
                }
                parts={parts}
                confirmed={confirmation === binding}
                disabled={!!busy}
                onChange={(next) => {
                  partsEdited.current = true;
                  setParts(next);
                  setConfirmation('');
                  setAnswer(null);
                }}
                onUseWholeQuestion={() => {
                  partsEdited.current = true;
                  setParts([question]);
                  setConfirmation('');
                  setAnswer(null);
                }}
                onRestoreSuggestions={() => {
                  partsEdited.current = false;
                  setParts(suggestQuestionParts(question).parts);
                  setConfirmation('');
                  setAnswer(null);
                }}
                onConfirm={(yes) => setConfirmation(yes ? binding : '')}
              />
              {!selectedModelLoaded && (
                <p className="text-sm text-muted-foreground">
                  Check the engine connection and choose the loaded local model in Workspace and
                  folders before asking. Source review stays available without a loaded model.
                </p>
              )}
              <button
                className={`${button} min-w-36`}
                disabled={!!busy || !canAsk}
                onClick={() => void ask()}
              >
                Get answer
              </button>
            </section>
          )}
          {answer && (
            <section
              ref={answerSection}
              tabIndex={-1}
              className="space-y-4 rounded-lg border border-border bg-card p-5 focus-visible:ring-2 focus-visible:ring-ring"
              aria-label="Your answer"
            >
              <h2 className="font-serif text-xl">3. Read the answer</h2>
              {!!answer.provenance?.reviewed_selection?.omitted_claims && (
                <p role="status" className="rounded-md border border-border p-3 text-sm">
                  {answer.provenance.reviewed_selection.selected_claims} reviewed statement
                  {answer.provenance.reviewed_selection.selected_claims === 1
                    ? ' was'
                    : 's were'}{' '}
                  supplied to this answer. {answer.provenance.reviewed_selection.omitted_claims}{' '}
                  other matching statement
                  {answer.provenance.reviewed_selection.omitted_claims === 1
                    ? ' was'
                    : 's were'}{' '}
                  left out to stay within the evidence limit. It may not cover every relevant
                  source.
                </p>
              )}
              <p className="text-sm font-medium">{answer.issue}</p>
              <p className="text-xs text-muted-foreground">
                {answer.review
                  ? 'A local review is recorded. The original evidence is retained; a review does not establish the real-world outcome.'
                  : 'Draft for review. A supported status is a model claim, not verified correctness. Unknown means the selected evidence does not establish an answer.'}
              </p>
              <a
                className="inline-block underline hover:text-primary focus-visible:ring-2 focus-visible:ring-ring"
                href={`#${reviewId}`}
                onClick={(event) => {
                  event.preventDefault();
                  const panel = document.getElementById(reviewId);
                  panel?.focus();
                  panel?.scrollIntoView({ block: 'start' });
                }}
              >
                Review and export this answer
              </a>
              {unavailableReview === reviewKey && (
                <p role="alert" className="text-sm text-destructive">
                  This answer is no longer available from the selected sources. Your review entries
                  are kept. Check and scan the folders, then reopen the answer before reviewing.
                </p>
              )}
              {effectiveBrief?.answers?.map((a) => (
                <div key={a.question_id} className="space-y-3">
                  <p className="text-sm font-medium">
                    {a.status === 'supported'
                      ? 'Supported by selected statements'
                      : a.status === 'unknown'
                        ? 'Not established by selected statements'
                        : 'Conflicting statements'}
                  </p>
                  <FocusedSourceAnswer
                    key={`${answer.id}:${a.question_id}`}
                    guidedReading
                    question={answer.review?.corrected_brief ? undefined : answer.issue}
                    answer={a}
                    evidence={answer.evidence}
                    readable={answer.readable_answers?.[a.question_id]}
                    outline={answer.answer_outlines?.[a.question_id]}
                    sourceLink={sourceLink}
                  />
                  {answer.review?.corrected_brief &&
                    (() => {
                      const citations = correctedAnswerCitations(a, answer.evidence);
                      return citations.length ? (
                        <div className="flex flex-wrap gap-3 text-xs" aria-label="Answer sources">
                          <span>Answer sources:</span>
                          {citations.map((citation) => (
                            <span key={citation}>{sourceLink(citation)}</span>
                          ))}
                        </div>
                      ) : null;
                    })()}
                </div>
              ))}
              {answer.review?.corrected_brief && (
                <details className="rounded-md border border-border p-4">
                  <summary className="cursor-pointer font-medium hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
                    Original draft before correction
                  </summary>
                  <div className="mt-4 space-y-4">
                    {answer.brief.answers?.map((original) => (
                      <FocusedSourceAnswer
                        key={original.question_id}
                        guidedReading
                        question={answer.issue}
                        answer={original}
                        evidence={answer.evidence}
                        readable={answer.readable_answers?.[original.question_id]}
                        outline={answer.answer_outlines?.[original.question_id]}
                        sourceLink={sourceLink}
                      />
                    ))}
                  </div>
                </details>
              )}
              <SuppliedEvidencePanel
                key={answer.id}
                ref={suppliedEvidence}
                answerId={answer.id}
                evidence={answer.evidence}
              />
              <GuidedAnswerReview
                key={`${reviewKey}:${answer.status}`}
                anchorId={reviewId}
                investigation={answer}
                disabled={!!busy || unavailableReview === reviewKey}
                onReview={reviewSavedAnswer}
                onExport={exportSavedAnswer}
                onAdvanced={() => onAdvanced(answer.issue, answer.id)}
              />
            </section>
          )}
          <section
            className="space-y-3 rounded-lg border border-border bg-card p-5"
            aria-label="Recent questions"
          >
            <h2 className="font-serif text-xl">Recent questions</h2>
            {recentLoading ? (
              <p role="status" className="text-sm">
                Loading saved questions…
              </p>
            ) : recentError ? (
              <p role="alert" className="text-sm text-destructive">
                {recentError}
              </p>
            ) : !recent.length ? (
              <p className="text-sm text-muted-foreground">
                Answers saved for this exact folder selection will appear here. Opening one does not
                run a model.
              </p>
            ) : (
              <ul className="space-y-2">
                {recent.slice(0, 20).map((item) => (
                  <li key={item.id}>
                    <button
                      className={`${button} w-full text-left`}
                      disabled={!!busy}
                      onClick={() => {
                        setAnswer(null);
                        void run('Opening saved answer…', async (ticket) => {
                          const data = await localApi.investigation(item.id, scope);
                          if (current(ticket)) {
                            updateSavedAnswer(data);
                            requestAnimationFrame(() => answerSection.current?.focus());
                          }
                        });
                      }}
                    >
                      {item.issue} · {item.status}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
