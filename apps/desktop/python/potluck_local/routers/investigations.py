"""Local-owner Company Brain investigation endpoints."""
from __future__ import annotations

import re
from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.investigations import (
    BriefValidationError, _privacy, calculation_preview, create_investigation, get_investigation, list_investigations,
    record_outcome, review_investigation, suggest_investigation_plan,
)
from potluck_local.services.memory_db import get_memory_db_path, init_memory_db
from potluck_local.services import investigation_readable_presentation as readable

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


class CalculationReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str
    status: Literal['calculate', 'missing', 'unsupported', 'conflicting']
    operand_ids: list[str] = Field(max_length=2)
    snippet_ids: list[str] = Field(max_length=8)
    reason: str = Field(min_length=1, max_length=1000)


class CalculationPreviewRequest(ScopeRequest):
    calculation_reviews: list[CalculationReview] = Field(min_length=1, max_length=12)


class ReviewRequest(ScopeRequest):
    decision: Literal['accept', 'correct', 'reject']
    reviewer: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=4000)
    corrected_brief: dict[str, Any] | None = None
    calculation_reviews: list[CalculationReview] | None = Field(default=None, min_length=1, max_length=12)


class OutcomeRequest(ScopeRequest):
    reviewer: str = Field(min_length=1, max_length=256)
    result: Literal['resolved', 'unresolved', 'inconclusive']
    notes: str = Field(min_length=1, max_length=4000)


class VerificationRequest(ScopeRequest):
    seed: int = Field(default=7, ge=0, le=4294967295, strict=True)
    version: Literal['draft-verification-v1', 'draft-verification-v2', 'draft-verification-v3'] = 'draft-verification-v1'


class RevisionPreviewRequest(ScopeRequest):
    group_id: str = Field(pattern=r'^G[1-9][0-9]*$', max_length=8)
    seed: int = Field(default=7, ge=0, le=4294967295, strict=True)


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
        return readable.decorate(await get_investigation(EvidenceScope(workspace, tuple(collection_ids)), iid))
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.post('/investigations/{iid}/calculation-preview')
async def preview_calculation(iid: str, body: CalculationPreviewRequest) -> dict[str, Any]:
    try:
        return await calculation_preview(body.scope(), iid, [row.model_dump() for row in body.calculation_reviews])
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.post('/investigations/{iid}/review')
async def review(iid: str, body: ReviewRequest) -> dict[str, Any]:
    try:
        return readable.decorate(await review_investigation(body.scope(), iid, body.decision, body.reviewer,
                                          body.reason, body.corrected_brief,
                                          [row.model_dump() for row in body.calculation_reviews] if body.calculation_reviews is not None else None))
    except BriefValidationError:
        raise HTTPException(status_code=400, detail='invalid_review_brief: Check the correction and exact source excerpts.') from None
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.post('/investigations/{iid}/draft-verification')
async def verify_draft(iid: str, body: VerificationRequest) -> dict[str, Any]:
    from potluck_local.services.investigation_verification import verify_investigation
    try:
        return await verify_investigation(body.scope(), iid, seed=body.seed, version=body.version)
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.post('/investigations/{iid}/revision-preview')
async def revision_preview(iid: str, body: RevisionPreviewRequest) -> dict[str, Any]:
    from potluck_local.services.investigation_revision import propose_revision
    try:
        return await propose_revision(body.scope(), iid, body.group_id, seed=body.seed)
    except (ValueError, PermissionError, LookupError) as exc:
        raise _error(exc) from None


@router.post('/investigations/{iid}/outcome')
async def outcome(iid: str, body: OutcomeRequest) -> dict[str, Any]:
    try:
        return readable.decorate(await record_outcome(body.scope(), iid, body.reviewer, body.result, body.notes))
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
async def export(iid: str, workspace: str, collection_ids: list[str] = Query(...),
                 layout: Literal['readable', 'original'] = 'readable') -> dict[str, str]:
    try:
        item = await get_investigation(EvidenceScope(workspace, tuple(collection_ids)), iid)
        def escape(value: Any) -> str:
            return re.sub(r'([\\`*_{}\[\]()#+.!<>|~-])', r'\\\1', str(value)).replace('\n', ' ')

        if layout == 'readable':
            markdown = readable.export(item, escape)
            if markdown is not None:
                return {'markdown': markdown}

        review = item['review']
        brief = review['corrected_brief'] if review and review['corrected_brief'] else item['brief']
        lines = ['# Investigation handoff', '', '## Issue', escape(item['issue']), '',
                 'Status: ' + escape(item['status']),
                 'Investigation: ' + escape(item['id']),
                 'Model: ' + escape(item['model_slug']),
                 'Model SHA256: ' + escape(item['model_sha']),
                 'Created: ' + escape(item['created_at']), '']
        provenance = item['provenance'] or {}
        from potluck_local.services.investigation_verification import validate_reports, report_sections
        checks = validate_reports(item)
        if checks:
            lines += ['## Local draft checks (unverified)',
                      'These assessments apply to the original draft and its cited passages only. '
                      'They do not approve the answer or evaluate owner corrections. '
                      'No issue found is not proof of correctness.', '']
            for report in checks:
                result = report['result']
                lines += ['### Check ' + escape(report['id']),
                          'Result: ' + escape(result['status']),
                          'Scope: ' + escape(result['scope'])]
                for warning in result.get('mechanical_warnings', []):
                    lines.append('- Mechanical heading warning (' + escape(warning['group_id']) + '): '
                                 + escape(warning['reason']))
                if 'checks' not in result:
                    lines += ['Reason: ' + escape(result['reason']), '']
                    continue
                for kind, entries in report_sections(report):
                    for entry in entries:
                        check = result['checks'][kind][entry['id']]
                        label = entry.get('text', entry.get('snippet_id', entry.get('focus', entry['id'])))
                        lines.append('- ' + escape(label) + ': ' + escape(check['verdict']) + '.'
                                     + (' ' + escape(check['reason']) if check['reason'] else ''))
                        if check.get('omitted_quote'):
                            lines.append('  - Omitted source text: ' + escape(check['omitted_quote']))
                        if kind == 'clause_coverage' and check['verdict'] == 'material_omission':
                            lines.append('  - Omitted source unit (' + escape(entry['id']) + ', '
                                         + escape(entry['snippet_id']) + '): ' + escape(entry['text']))
                lines.append('')
        lines += ['Workspace: ' + escape(item['workspace']),
                  'Collections: ' + ', '.join(escape(cid) for cid in item['collection_ids']),
                  'Template: ' + escape(provenance.get('template_version', 'unavailable')),
                  'Retrieval: ' + escape(provenance.get('retrieval_version', 'unavailable')),
                  'Generation settings: ' + escape(provenance.get('generation_settings', {})), '']
        if 'questions' in brief:
            lines += ['## Questions and answers']
            if provenance.get('template_version') in ('investigation-v14', 'investigation-v15', 'investigation-v16', 'investigation-v17', 'investigation-v18', 'investigation-v19', 'investigation-v20', 'investigation-v21'):
                for warning in item['brief'].get('model_uncertainties', []):
                    lines += ['**Original model output warning:** ' + escape(warning), '']
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
        if provenance.get('template_version') in ('investigation-v14', 'investigation-v15', 'investigation-v16', 'investigation-v17', 'investigation-v18', 'investigation-v19', 'investigation-v20', 'investigation-v21'):
            heading = ('## Original model-selected passages and server-added structural context (unverified)' if provenance.get('template_version') == 'investigation-v21'
                       else '## Final machine-reviewed claims (unverified)' if provenance.get('template_version') == 'investigation-v17'
                       else '## Original model-selected source passages (unverified)' if provenance.get('template_version') in ('investigation-v18', 'investigation-v19', 'investigation-v20', 'investigation-v21')
                       else '## Original machine claims (unverified)')
            detail = ('These are the original model-selected passages and labeled server-added structural context, separate from any owner correction.' if provenance.get('template_version') == 'investigation-v21' else
                      'These are the original model-selected source passages and citations, separate from any owner correction.'
                      if provenance.get('template_version') in ('investigation-v18', 'investigation-v19', 'investigation-v20', 'investigation-v21') else
                      'These are the original model claims and citations, separate from any owner correction.')
            lines += [heading, detail, '']
            if provenance.get('template_version') in ('investigation-v19', 'investigation-v20', 'investigation-v21'):
                lines += ['### Model-proposed request focuses (unverified)',
                          'The aggregate status does not identify which focus is unresolved; these model assessments do not verify coverage.', '']
                focus_groups = provenance.get('answer_coverage', {}).get('focus_groups', {})
                for question in item['brief'].get('questions', []):
                    group = focus_groups.get(question['id'])
                    if group is None:
                        continue
                    lines.append('#### ' + escape(question['text']))
                    lines.append('Aggregate assessment (unverified): ' + escape(group['aggregate_status']) +
                                 ' · ' + escape(group['status_derivation']))
                    lines.append('Model reported capacity leaves an aspect uncovered: ' + ('yes' if group['model_reported_more'] else 'no'))
                    for focus in group['groups']:
                        ids = ', '.join(escape(sid) for sid in focus['snippet_ids']) or 'none'
                        lines.append('- ' + escape(focus['focus']) + ' · ' + escape(focus['status']) + ' · Source IDs: ' + ids)
                    lines.append('')
            conclusion_trace = provenance.get('answer_coverage', {}).get('cited_conclusions')
            if conclusion_trace is not None:
                lines += ['### Original cited conclusions (unverified)',
                          'Model-written conclusions are separate from exact source quotations and any owner correction.', '']
                for request_id, entries in conclusion_trace['operations'].items():
                    for entry in entries:
                        if not entry['text']:
                            continue
                        lines += ['#### Request ' + escape(request_id) + ' · ' + escape(entry['focus']),
                                  escape(entry['text'])]
                        for excerpt in entry['source_excerpts']:
                            lines.append('- Supporting source (' + escape(excerpt['citation_id']) + '): ' + escape(excerpt['quote']))
                        lines.append('')
            unknown_trace = provenance.get('answer_coverage', {}).get('unknown_explanations')
            component_entries = [(rid, entry) for rid, entries in (conclusion_trace or {}).get('operations', {}).items()
                                 for entry in entries if 'components' in entry]
            if component_entries:
                lines += ['### Original answer components (unverified)',
                          'The server assembles these model-written parts without dropping captured qualifications. '
                          'Source binding does not prove completeness or entailment.', '']
                for rid, entry in component_entries:
                    lines += ['#### Request ' + escape(rid) + ' · ' + escape(entry['focus']),
                              'Primary answer: ' + escape(entry['primary_conclusion']),
                              'Missing fact: ' + escape(entry['primary_unknown_reason'])]
                    for kind, values in entry['components'].items():
                        for value in values:
                            lines.append('- ' + kind.capitalize() + ': ' + escape(value['text']) +
                                         ' · Source IDs: ' + ', '.join(escape(s) for s in value['snippet_ids']))
                    lines.append('')
            if unknown_trace is not None:
                lines += ['### Original missing-evidence explanations (unverified)',
                          'These model assessments and their cited passages remain separate from any owner correction.', '']
                for request_id, entries in unknown_trace['operations'].items():
                    for entry in entries:
                        lines += ['#### Request ' + escape(request_id) + ' · ' + escape(entry['focus']),
                                  escape(entry['text'])]
                        for excerpt in entry['source_excerpts']:
                            lines.append('- Related source (' + escape(excerpt['citation_id']) + '): ' + escape(excerpt['quote']))
                        if not entry['source_excerpts']:
                            lines.append('No related source passage was selected; absence is not verified.')
                        lines.append('')
            if provenance.get('template_version') == 'investigation-v21':
                operations = provenance.get('answer_coverage', {}).get('structural_context', {}).get('operations', {})
                lines += ['### Original structural context (unverified)', '']
                for request_id, structure in operations.items():
                    lines.append('#### Request ' + escape(request_id))
                    lines.append('- Model-selected IDs: ' + (', '.join(escape(sid) for sid in structure['model_selected_ids']) or 'none'))
                    lines.append('- Server-added structural context IDs: ' + (', '.join(escape(sid) for sid in structure['server_added_ids']) or 'none'))
                    lines.append('- Structural context incomplete or ambiguous: ' + ('yes' if structure['incomplete'] else 'no'))
                    lines.append('')
            for answer in item['brief'].get('answers', []):
                for part in answer.get('parts', []):
                    lines.append('### ' + escape(part['id']) + ' · ' + escape(part['request_span']))
                    if provenance.get('template_version') == 'investigation-v15':
                        lines.append('Model-proposed aspect (unverified): ' + escape(part['aspect']))
                        for excerpt in part['planned_excerpts']:
                            lines.append('- Model-selected planning excerpt (' + escape(excerpt['citation_id']) + '): ' + escape(excerpt['quote']))
                    for claim in part['claims']:
                        lines.append('- ' + escape(claim['id']) + ' · ' + escape(claim['status']) + ': ' + escape(claim['text']))
                        for excerpt in claim['source_excerpts']:
                            lines.append('  - Source excerpt (' + escape(excerpt['citation_id']) + '): ' + escape(excerpt['quote']))
                    for aspect in part['unmet_aspects']:
                        lines.append('- Model-reported unmet aspect: ' + escape(aspect))
                    lines.append('')
        if provenance.get('template_version') == 'investigation-v17':
            machine = provenance.get('machine_source_review') or {}
            initial = machine.get('initial_brief') or {}
            lines += ['## Initial machine draft (unverified)',
                      'This initial draft and the later machine assessment are not human review.', '']
            for warning in initial.get('model_uncertainties', []):
                lines.append('- ' + escape(warning))
            for answer in initial.get('answers', []):
                lines.append('### ' + escape(answer['question_id']) + ' · ' + escape(answer['status']))
                if 'parts' not in answer:
                    lines.append(escape(answer['text']))
                    for excerpt in answer.get('source_excerpts', []):
                        lines.append('- Source excerpt (' + escape(excerpt['citation_id']) + '): ' + escape(excerpt['quote']))
                for part in answer.get('parts', []):
                    for claim in part['claims']:
                        lines.append('- ' + escape(claim['id']) + ' · ' + escape(claim['status']) + ': ' + escape(claim['text']))
                        for excerpt in claim['source_excerpts']:
                            lines.append('  - Source excerpt (' + escape(excerpt['citation_id']) + '): ' + escape(excerpt['quote']))
                lines.append('')
        if provenance.get('template_version') == 'investigation-v9':
            lines += ['## Plan acknowledgement', escape(provenance.get('operation_plan_origin', 'unavailable')),
                      'Plan confirmation is not approval of the answers.', '', '## Quantity extraction notices']
            for notice in provenance.get('extraction_notices', []):
                lines.append('- ' + escape(notice['snippet_id']) + ' (' + escape(notice['citation_id']) +
                             '): ' + escape(notice['source_text']) + ' · ' + escape(notice['reason']))
            lines.append('')
        formulas = provenance.get('formula_calculations')
        if formulas:
            lines += ['## Original source formula calculations',
                      'These calculations belong to the original draft. User scenario values are distinct from document facts.', '']
            for trace in formulas['traces']:
                lines += ['### ' + escape(trace['request_id']), 'Status: ' + escape(trace['status'])]
                if trace['status'] == 'computed':
                    formula = trace['formula']
                    lines += ['Source formula (' + escape(formula['snippet_id']) + '): ' + escape(formula['source_text']),
                              'Calculation: ' + escape(trace['evaluated_expression']) + ' = ' + escape(trace['result']) + '%',
                              'Limit: ' + escape(trace['caveat'])]
                    for role, bindings in trace['bindings'].items():
                        lines.append('- Computed ' + escape(role) + ': ' + escape(trace['computed'][role]))
                        for variable, value in bindings.items():
                            origin = 'user scenario' if value['snippet_id'] == 'user-issue' else value['snippet_id']
                            lines.append('  - ' + escape(variable) + ': ' + escape(value['source_text']) + ' (' + escape(origin) + ')')
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
        if review and review.get('calculation_reviews'):
            lines += ['## Reviewed calculation choices', 'Reviewer: ' + escape(review['reviewer']), '']
            for choice in review['calculation_reviews']:
                trace = choice['trace']
                lines += ['### ' + escape(choice['request_id']),
                          'Selection: ' + escape(choice['status']),
                          'Inputs: ' + escape(', '.join(choice['operand_ids']) or 'none'),
                          'Reason: ' + escape(choice['reason']),
                          'Result: ' + escape(trace['text']),
                          'Algorithm: ' + escape(trace.get('algorithm_version') or 'unavailable'),
                          'Formula: ' + escape(trace.get('formula') or 'unavailable'),
                          'Evaluated expression: ' + escape(trace.get('evaluated_expression') or 'unavailable'),
                          'Rounding: ' + escape(trace.get('rounding') or 'unavailable'),
                          'Limit: ' + escape(trace.get('caveat') or 'unavailable')]
                for operand in trace.get('normalized_inputs', []):
                    lines.append('- Normalized ' + escape(operand['role']) + ' (' + escape(operand['id']) +
                                 '): ' + escape(operand['value']) + ' ' + escape(operand['unit']) +
                                 ' · conversion factor ' + escape(operand['conversion_factor']))
                by_snippet = {s['snippet_id']: s for s in provenance.get('snippet_catalog', [])}
                for sid in choice['snippet_ids']:
                    excerpt = by_snippet[sid]
                    lines.append('- Source excerpt (' + escape(excerpt['citation_id']) + '): ' + escape(excerpt['quote']))
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
        if provenance.get('input_budget'):
            budget = provenance['input_budget']
            if budget.get('version') in ('fixed-part-necessity-review-v28','fixed-part-necessity-review-v29','fixed-part-necessity-review-v30','fixed-part-edit-review-v31','fixed-part-focused-review-v32'):
                lines += ['## Measured model input']
                for stage in budget['stages']:
                    lines += ['Stage: ' + escape(stage['stage']),
                        'Input tokens: ' + escape(stage['input_tokens']),
                        'Reserved output tokens: ' + escape(stage['output_tokens']),
                        'Context capacity: ' + escape(stage['context_tokens'])]
            else:
                lines += ['## Measured model input',
                    'Input tokens: ' + escape(budget['input_tokens']),
                    'Reserved output tokens: ' + escape(budget['output_tokens']),
                    'Context capacity: ' + escape(budget['context_tokens'])]
            if budget.get('version') in ('fixed-part-necessity-review-v28','fixed-part-necessity-review-v29','fixed-part-necessity-review-v30','fixed-part-edit-review-v31','fixed-part-focused-review-v32'):
                first_stage = budget['stages'][0]
                lines += ['Presented reviewed claim IDs: ' + escape(', '.join(first_stage['model_visible_claim_ids']) or 'none'),
                          'Source visibility: ' + escape(first_stage['support_visibility'])]
            elif budget.get('version') in ('reviewed-claim-whole-v1', 'fixed-part-single-review-v23', 'fixed-part-single-review-v24', 'fixed-part-single-review-v25', 'fixed-part-single-review-v26', 'fixed-part-single-review-v27'):
                lines += ['Presented reviewed claim IDs: ' + escape(', '.join(budget['model_visible_claim_ids']) or 'none'),
                          'Source visibility: ' + escape(budget['support_visibility'])]
            else:
                lines += ['Presented passage IDs: ' + escape(', '.join(budget['selected_snippet_ids']) or 'none'),
                          'Omitted passage IDs: ' + escape(', '.join(budget['omitted_snippet_ids']) or 'none'),
                          'Selection: ' + escape(budget['selection'])]
            lines.append('')
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
