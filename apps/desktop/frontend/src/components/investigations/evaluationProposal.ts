import type { Investigation } from '@/types/investigations';

export function bindEvaluationProposal(item: Investigation, value: Record<string, unknown>) {
  if (
    !Array.isArray(item.collection_ids) ||
    !Array.isArray(item.evidence) ||
    item.status !== 'reviewed' ||
    !item.review ||
    !item.outcome ||
    !/^[a-f0-9]{64}$/i.test(item.model_sha ?? '')
  )
    throw new Error('Reopen a reviewed answer with a recorded outcome and model identity.');
  const citations = [...new Set(item.evidence.map((e) => e.citation_id))].sort();
  if (citations.some((id) => typeof id !== 'string' || !id))
    throw new Error('The saved source references need review. Reopen this answer.');
  const expected = {
    investigation_id: item.id,
    workspace: item.workspace,
    collection_ids: [...item.collection_ids].sort(),
    citation_ids: citations,
    review_decision: item.review.decision,
    outcome_result: item.outcome.result,
    model_slug: item.model_slug,
    model_sha: item.model_sha,
    status: 'proposed',
  };
  if (
    !value ||
    Object.keys(value).length !== Object.keys(expected).length ||
    Object.entries(expected).some(
      ([key, actual]) => JSON.stringify(value[key]) !== JSON.stringify(actual),
    )
  )
    throw new Error(
      'The proposal does not match this saved answer. Reopen it before trying again.',
    );
  return expected;
}
