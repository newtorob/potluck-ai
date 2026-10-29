"""Local-owner Company Brain investigation endpoints."""
from __future__ import annotations

import re
from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.investigations import (
    BriefValidationError, _privacy, create_investigation, get_investigation, list_investigations,
    record_outcome, review_investigation, suggest_investigation_plan,
)
from potluck_local.services.memory_db import get_memory_db_path, init_memory_db

router = APIRouter(prefix='/company-brain', tags=['company-brain'])


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workspace: str
    collection_ids: list[str] = Field(min_length=1, max_length=100)

    def scope(self) -> EvidenceScope:
        return EvidenceScope(self.workspace, tuple(self.collection_ids))


class ReviewedOperation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request: str = Field(min_length=1, max_length=500)
    operation: Literal['answer', 'difference', 'percentage_point_difference', 'percent_change', 'percentage']


class PlanRequest(ScopeRequest):
    issue: str = Field(min_length=1, max_length=4000)
    model_slug: str = Field(min_length=1, max_length=128)
    generation_profile: str | None = Field(default=None, min_length=1, max_length=128)


class CreateRequest(ScopeRequest):
    issue: str = Field(min_length=1, max_length=4000)
    query: str = Field(min_length=1, max_length=1000)
    model_slug: str = Field(min_length=1, max_length=128)
    answer_each_question: StrictBool = False
    questions: list[str] | None = Field(default=None, min_length=1, max_length=8)
    generation_profile: str | None = Field(default=None, min_length=1, max_length=128)
    reviewed_operations: list[ReviewedOperation] | None = Field(default=None, min_length=1, max_length=12)
    plan_confirmed: StrictBool = False


class ReviewRequest(ScopeRequest):
    decision: Literal['accept', 'correct', 'reject']
    reviewer: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=4000)
    corrected_brief: dict[str, Any] | None = None


class OutcomeRequest(ScopeRequest):
    reviewer: str = Field(min_length=1, max_length=256)
    result: Literal['resolved', 'unresolved', 'inconclusive']
    notes: str = Field(min_length=1, max_length=4000)


class ProposalRequest(ScopeRequest):
    owner_confirmed: StrictBool


class KnowledgeProposalRequest(ProposalRequest):
    claim_key: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=16000)
    citation_ids: list[str] = Field(min_length=1, max_length=16)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail='Evidence unavailable')
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail='Investigation unavailable')
    message = str(exc)
    code = ('model_not_loaded' if message in ('Requested model is not loaded locally',
                                              'Loaded model identity is unavailable') else
            'profile_incompatible' if message == 'Investigation profile incompatible' else
            'collections_unavailable' if message == 'All selected collections must be ready and recent' else
            'generation_timeout' if message == 'Investigation generation timed out' else
            'inference_failed' if message == 'Investigation inference failed' else
            'output_limit' if message == 'Investigation output limit reached' else
            'invalid_format' if message == 'Invalid model format' else
            'invalid_citation' if message == 'Invalid model citation' else
            'request_failed')
    messages = {
        'model_not_loaded': 'Load the selected local model in Models, then retry.',
        'profile_incompatible': 'The selected generation profile requires its verified model template and backend.',
        'collections_unavailable': 'Scan every selected collection successfully before continuing.',
        'inference_failed': 'Local inference failed. No investigation was saved.',
        'invalid_format': 'The model returned an invalid brief format. No investigation was saved.',
        'output_limit': 'The model reached its output limit. No investigation was saved.',
        'invalid_citation': 'The model cited evidence outside this request. No investigation was saved.',
        'generation_timeout': 'The local model exceeded the time limit. No investigation was saved.',
        'request_failed': 'Check the request and source freshness, then retry.',
    }
    return HTTPException(status_code=400, detail=f'{code}: {messages[code]}')


@router.get('/collections')
async def collections(workspace: str) -> dict[str, Any]:
    try:
        _privacy()
        await init_memory_db()
        if not workspace.strip() or workspace != workspace.strip():
            raise ValueError
        async with aiosqlite.connect(get_memory_db_path()) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('SELECT id,name,scan_status,last_scan_at FROM source_collections '
                                      'WHERE workspace=? ORDER BY name LIMIT 100', (workspace,))
            return {'collections': [dict(row) for row in await cursor.fetchall()]}
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.get('/investigations')
async def list_items(workspace: str, collection_ids: list[str] = Query(...)) -> dict[str, Any]:
    try:
        return {'investigations': await list_investigations(EvidenceScope(workspace, tuple(collection_ids)))}
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.post('/investigations')
async def create(body: CreateRequest) -> dict[str, Any]:
    try:
        if body.reviewed_operations is not None:
            if not body.plan_confirmed or body.answer_each_question or body.questions is not None:
                raise ValueError('A confirmed plan cannot be combined with question options')
            return await create_investigation(body.scope(), body.issue, body.query, body.model_slug,
                                              template_version='investigation-v9',
                                              reviewed_operations=[r.model_dump() for r in body.reviewed_operations],
                                              generation_profile=body.generation_profile)
        if body.plan_confirmed:
            raise ValueError('Confirmation requires an operation plan')
        if not body.answer_each_question and (body.questions is not None or body.generation_profile is not None):
            raise ValueError('Question options require v6')
        if body.answer_each_question:
            return await create_investigation(body.scope(), body.issue, body.query, body.model_slug,
                                              template_version='investigation-v6', questions=body.questions,
                                              generation_profile=body.generation_profile)
        return await create_investigation(body.scope(), body.issue, body.query, body.model_slug)
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.post('/investigations/plan')
async def plan(body: PlanRequest) -> dict[str, Any]:
    try:
        return await suggest_investigation_plan(body.scope(), body.issue, body.model_slug,
                                                generation_profile=body.generation_profile)
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.get('/investigations/{iid}')
async def get(iid: str, workspace: str, collection_ids: list[str] = Query(...)) -> dict[str, Any]:
    try:
        return await get_investigation(EvidenceScope(workspace, tuple(collection_ids)), iid)
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.post('/investigations/{iid}/review')
async def review(iid: str, body: ReviewRequest) -> dict[str, Any]:
    try:
        return await review_investigation(body.scope(), iid, body.decision, body.reviewer,
                                          body.reason, body.corrected_brief)
    except BriefValidationError:
        raise HTTPException(status_code=400, detail='invalid_review_brief: Check the correction and exact source excerpts.') from None
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.post('/investigations/{iid}/outcome')
async def outcome(iid: str, body: OutcomeRequest) -> dict[str, Any]:
    try:
        return await record_outcome(body.scope(), iid, body.reviewer, body.result, body.notes)
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.post('/investigations/{iid}/evaluation-case')
async def evaluation_case(iid: str, body: ProposalRequest) -> dict[str, Any]:
    from potluck_local.services.investigation_evaluation import propose_evaluation_case
    try:
        return await propose_evaluation_case(body.scope(), iid,
                                             owner_confirmed=body.owner_confirmed)
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.post('/investigations/{iid}/knowledge-proposal')
async def knowledge_proposal(iid: str, body: KnowledgeProposalRequest) -> dict[str, str]:
    from potluck_local.services.investigation_evaluation import propose_knowledge_update
    try:
        claim_id = await propose_knowledge_update(
            body.scope(), iid, body.claim_key, body.content, body.citation_ids,
            owner_confirmed=body.owner_confirmed)
        return {'claim_id': claim_id, 'status': 'proposed'}
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.get('/investigations/{iid}/export')
async def export(iid: str, workspace: str, collection_ids: list[str] = Query(...)) -> dict[str, str]:
    try:
        item = await get_investigation(EvidenceScope(workspace, tuple(collection_ids)), iid)
        def escape(value: Any) -> str:
            return re.sub(r'([\\`*_{}\[\]()#+.!<>|~-])', r'\\\1', str(value)).replace('\n', ' ')

        review = item['review']
        brief = review['corrected_brief'] if review and review['corrected_brief'] else item['brief']
        lines = ['# Investigation handoff', '', '## Issue', escape(item['issue']), '',
                 'Status: ' + escape(item['status']),
                 'Investigation: ' + escape(item['id']),
                 'Model: ' + escape(item['model_slug']),
                 'Model SHA256: ' + escape(item['model_sha']),
                 'Created: ' + escape(item['created_at']), '']
        provenance = item['provenance'] or {}
        lines += ['Workspace: ' + escape(item['workspace']),
                  'Collections: ' + ', '.join(escape(cid) for cid in item['collection_ids']),
                  'Template: ' + escape(provenance.get('template_version', 'unavailable')),
                  'Retrieval: ' + escape(provenance.get('retrieval_version', 'unavailable')),
                  'Generation settings: ' + escape(provenance.get('generation_settings', {})), '']
        if 'questions' in brief:
            lines += ['## Questions and answers']
            by_id = {a['question_id']: a for a in brief['answers']}
            for question in brief['questions']:
                answer = by_id[question['id']]
                lines += ['### ' + escape(question['text']),
                          escape(answer['status']) + ': ' + escape(answer['text'])]
                if answer['status'] == 'unknown':
                    lines.append('This uncertainty is limited to the presented evidence.')
                for excerpt in answer['source_excerpts']:
                    lines.append('- Source excerpt (' + escape(excerpt['citation_id']) + '): ' + escape(excerpt['quote']))
                lines.append('')
        if provenance.get('template_version') == 'investigation-v9':
            lines += ['## Plan acknowledgement', escape(provenance.get('operation_plan_origin', 'unavailable')),
                      'Plan confirmation is not approval of the answers.', '', '## Quantity extraction notices']
            for notice in provenance.get('extraction_notices', []):
                lines.append('- ' + escape(notice['snippet_id']) + ' (' + escape(notice['citation_id']) +
                             '): ' + escape(notice['source_text']) + ' · ' + escape(notice['reason']))
            lines.append('')
        traces = provenance.get('calculation_traces', [])
        if traces:
            lines += ['## Original machine calculations',
                      'These calculations belong to the original draft; reviewer corrections are separate.', '']
            requests = {r['id']: r['request'] for r in provenance.get('operation_plan', [])}
            for trace in traces:
                lines += ['### ' + escape(trace['request_id']) + ' · ' + escape(trace['operation']),
                          'Original request: ' + escape(requests.get(trace['request_id'], 'unavailable')),
                          'Status: ' + escape(trace['status']),
                          'Algorithm: ' + escape(trace.get('algorithm_version') or 'unavailable'),
                          'Formula: ' + escape(trace.get('formula') or 'unavailable'),
                          'Evaluated expression: ' + escape(trace.get('evaluated_expression') or 'unavailable'),
                          'Result: ' + (escape(trace['result']) +
                                       (' ' + escape(trace['unit']) if trace.get('unit') else '')
                                       if trace.get('result') is not None else 'unavailable'),
                          'Rounding: ' + escape(trace.get('rounding') or 'unavailable'),
                          'Reason: ' + escape(trace.get('reason') or 'none'),
                          'Limit: ' + escape(trace.get('caveat') or 'unavailable')]
                for operand in trace.get('inputs', []):
                    lines.append('- Input ' + escape(operand['id']) + ' (' + escape(operand['citation_id']) +
                                 ', ' + escape(operand['snippet_id']) + '): ' + escape(operand['source_text']))
                    if 'unit_source_text' in operand:
                        lines.append('  - Shared unit: ' + escape(operand['unit_source_text']) +
                                     ' · snippet offsets ' + escape(operand['unit_source_start']) +
                                     '–' + escape(operand['unit_source_end']))
                for operand in trace.get('normalized_inputs', []):
                    lines.append('- Normalized ' + escape(operand['role']) + ' (' + escape(operand['id']) +
                                 '): ' + escape(operand['value']) + ' ' + escape(operand['unit']) +
                                 ' · conversion factor ' + escape(operand['conversion_factor']))
                lines.append('')
        for key, title in [('facts', 'Facts'), ('prior_fixes', 'Prior fixes'),
                           ('hypotheses', 'Hypotheses (unverified)'), ('recommended_checks', 'Recommended checks')]:
            lines += ['## ' + title]
            for entry in brief[key]:
                cites = ', '.join(escape(cid) for cid in entry['citation_ids']) or 'uncited'
                lines.append('- ' + escape(entry['text']) + ' (' + cites + ')')
                for excerpt in entry.get('source_excerpts', []):
                    lines.append('  - Source excerpt (' + escape(excerpt['citation_id']) + '): ' + escape(excerpt['quote']))
            if not brief[key]:
                lines.append('- None recorded')
            lines.append('')
        if 'missing_evidence' in brief:
            lines += ['## Historical combined missing evidence (provenance unknown)'] + ['- ' + escape(x) for x in brief['missing_evidence']] + ['']
        if 'model_uncertainties' in brief:
            lines += ['## Model questions (unverified)'] + ['- ' + escape(x) for x in brief['model_uncertainties']] + ['']
        lines += ['## Retrieval notices'] + ['- ' + escape(x) for x in item.get('retrieval_notices', provenance.get('retrieval_notices', []))]
        lines += ['', '## Evidence provenance']
        for evidence in item['evidence']:
            lines.append('- ' + escape(evidence['citation_id']) + ' · ' +
                         escape(evidence.get('relative_path', 'Reviewed claim')) +
                         ' · ' + escape(evidence.get('anchor', '')))
            sources = evidence['sources'] if evidence['kind'] == 'reviewed_claim' else [evidence]
            for source in sources:
                lines.append('  - ' + escape(source.get('relative_path', source['document_id'])) +
                             ' · revision ' + escape(source['revision_id']) +
                             ' · chunk ' + escape(source['chunk_id']))
        lines += ['', '## Review']
        lines.append(escape(f"{review['decision']} by {review['reviewer']}: {review['reason']}")
                     if review else 'Not reviewed')
        if review and review['corrected_brief']:
            lines.append('The sections above use the reviewed correction. The original draft is retained in Potluck.')
        lines += ['', '## Actual outcome']
        outcome = item['outcome']
        lines.append(escape(f"{outcome['result']} by {outcome['reviewer']}: {outcome['notes']}")
                     if outcome else 'Not recorded')
        return {'markdown': '\n'.join(lines) + '\n'}
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None
