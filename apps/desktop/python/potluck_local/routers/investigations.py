"""Local-owner Company Brain investigation endpoints."""
from __future__ import annotations

import re
from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.investigations import (
    _privacy, create_investigation, get_investigation, list_investigations,
    record_outcome, review_investigation,
)
from potluck_local.services.memory_db import get_memory_db_path, init_memory_db

router = APIRouter(prefix='/company-brain', tags=['company-brain'])


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workspace: str
    collection_ids: list[str] = Field(min_length=1, max_length=100)

    def scope(self) -> EvidenceScope:
        return EvidenceScope(self.workspace, tuple(self.collection_ids))


class CreateRequest(ScopeRequest):
    issue: str = Field(min_length=1, max_length=4000)
    query: str = Field(min_length=1, max_length=1000)
    model_slug: str = Field(min_length=1, max_length=128)


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
            'collections_unavailable' if message == 'All selected collections must be ready and recent' else
            'generation_timeout' if message == 'Investigation generation timed out' else
            'inference_failed' if message == 'Investigation inference failed' else
            'output_limit' if message == 'Investigation output limit reached' else
            'invalid_format' if message == 'Invalid model format' else
            'invalid_citation' if message == 'Invalid model citation' else
            'request_failed')
    messages = {
        'model_not_loaded': 'Load the selected local model in Models, then retry.',
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
        return await create_investigation(body.scope(), body.issue, body.query, body.model_slug)
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
        for key, title in [('facts', 'Facts'), ('prior_fixes', 'Prior fixes'),
                           ('hypotheses', 'Hypotheses'), ('recommended_checks', 'Recommended checks')]:
            lines += ['## ' + title]
            for entry in brief[key]:
                cites = ', '.join(escape(cid) for cid in entry['citation_ids']) or 'uncited'
                lines.append('- ' + escape(entry['text']) + ' (' + cites + ')')
            if not brief[key]:
                lines.append('- None recorded')
            lines.append('')
        lines += ['## Missing evidence'] + ['- ' + escape(x) for x in brief['missing_evidence']]
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
