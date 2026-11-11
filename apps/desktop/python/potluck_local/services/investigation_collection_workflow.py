"""Opt-in retrieval of scoped indexed evidence for a detached workflow proposal."""
from copy import deepcopy
import hashlib
import json

import aiosqlite

from . import investigation_evidence_workflow as workflow
from . import investigation_evidence_plan as evidence_plan
from . import investigation_evidence_revision as evidence_revision
from . import investigation_revision as revision
from .evidence_lookup import EvidenceScope, _ready, lookup_evidence, resolve_citation
from .investigations import _privacy
from .memory_db import get_memory_db_path

VERSION = 'collection-evidence-workflow-v2'
MAX_RESULTS = 10
MAX_DOCUMENTS = 1000
SCOPE = 'selected retrieved excerpts from the specified indexed snapshot; not collection-wide proof of absence'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


async def _snapshot(scope):
    if not isinstance(scope, EvidenceScope):
        raise ValueError('Explicit evidence scope required')
    async with aiosqlite.connect(get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN')
        ready, stale = await _ready(db, scope)
        if stale or set(ready) != set(scope.collection_ids):
            raise ValueError('Selected collections must be ready and recent')
        ph = ','.join('?' for _ in scope.collection_ids)
        cur = await db.execute(
            'SELECT id,name,includes,excludes,scan_status,last_scan_at FROM source_collections '
            f'WHERE workspace=? AND id IN ({ph}) ORDER BY id',
            (scope.workspace, *scope.collection_ids),
        )
        collections = [dict(row) for row in await cur.fetchall()]
        cur = await db.execute(
            'SELECT collection_id,id,relative_path,content_sha,revision_id,parser_version '
            f'FROM source_documents WHERE collection_id IN ({ph}) '
            'ORDER BY collection_id,id LIMIT ?',
            (*scope.collection_ids, MAX_DOCUMENTS + 1),
        )
        documents = [dict(row) for row in await cur.fetchall()]
        if len(documents) > MAX_DOCUMENTS:
            raise ValueError('Selected snapshot exceeds document limit')
    return {'workspace': scope.workspace, 'collection_ids': list(scope.collection_ids),
            'collections': collections, 'documents': documents}


async def prepare(scope, question):
    """Return a replayable receipt; preparation failures never clip source evidence."""
    _privacy()
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        raise ValueError('Invalid question')
    query, query_trace = evidence_plan.bounded_query(question)
    before = await _snapshot(scope)
    result = await lookup_evidence(scope, query, MAX_RESULTS, retrieval_version='v2')
    if before != await _snapshot(scope):
        raise ValueError('Evidence snapshot changed')
    if result['scope_status']['ready'] != len(scope.collection_ids):
        raise ValueError('Selected collections must be ready and recent')
    records = []
    for record in result['source_chunks']:
        if await resolve_citation(scope, record['citation_id']) != record:
            raise ValueError('Evidence citation changed')
        records.append(deepcopy(record))
    if before != await _snapshot(scope):
        raise ValueError('Evidence snapshot changed')
    if not records:
        raise ValueError('No source excerpts available for workflow preparation')
    def make_envelope(selected):
        return {'version': revision.VERSION, 'scope': SCOPE, 'single_focus': True,
                'group': {'id': 'G1', 'focus': question, 'request': question,
                          'status': 'unknown', 'draft_sentences': []},
                'sources': [
                    {'id': f'Q{i}', 'snippet_id': f'S{i}', 'quote': r['content'],
                     'context': {'citation_id': r['citation_id'], 'heading': r['heading'],
                                 'list_introduction': None,
                                 'cited_chunk': {'quote': r['content'], 'selected_start': 0,
                                                 'selected_end': len(r['content'])}}}
                    for i, r in enumerate(selected, 1)]}

    selected, omitted = [], []
    prepared = envelope = None
    for record in records:
        candidate = selected + [record]
        proposed = make_envelope(candidate)
        try:
            candidate_prepared = workflow.prepare(proposed, version=workflow.V10_VERSION)
        except (ValueError, TypeError, KeyError):
            omitted.append({'citation_id': record['citation_id'],
                            'reason': 'workflow_preparation_limit_or_unsupported'})
            continue
        selected, envelope, prepared = candidate, proposed, candidate_prepared
    if not selected:
        raise ValueError('No retrieved whole excerpts fit workflow preparation limits')
    body = {'version': VERSION, 'question': question, 'query': query, 'query_trace': query_trace,
            'scope': SCOPE, 'snapshot': before, 'ranked_candidates': records, 'sources': selected,
            'packing': {'candidate_count': len(records), 'selected_count': len(selected),
                        'omitted': omitted, 'max_retrieval_results': MAX_RESULTS,
                        'max_source_chars': evidence_revision.MAX_SOURCE_CHARS,
                        'max_source_units': evidence_revision.MAX_UNITS,
                        'policy': 'whole chunks in retrieval rank order; no clipping'},
            'retrieval': {'truncated': result['truncated'], 'trace': result.get('retrieval_trace'),
                          'notices': result['missing_evidence'], 'reviewed_claims_omitted': len(result['reviewed_claims'])},
            'envelope': envelope, 'prepared_workflow': prepared}
    return {**body, 'receipt_sha256': _digest(body)}


async def generate(scope, receipt, runtime, *, model, model_sha, seed=7):
    """Rebuild the receipt before and after generation; reject stale or modified inputs."""
    if not isinstance(receipt, dict) or receipt.get('version') != VERSION:
        raise ValueError('Invalid collection workflow receipt')
    try:
        body = {k: v for k, v in receipt.items() if k != 'receipt_sha256'}
        if _digest(body) != receipt['receipt_sha256'] or await prepare(scope, receipt['question']) != receipt:
            raise ValueError('Collection workflow receipt changed')
        result = await workflow.generate(deepcopy(receipt['prepared_workflow']), runtime,
                                         model=model, model_sha=model_sha, seed=seed)
        if await prepare(scope, receipt['question']) != receipt:
            raise ValueError('Evidence snapshot changed')
        return {'version': VERSION, 'attempt': result,
                'provenance': {'receipt_sha256': receipt['receipt_sha256'],
                               'snapshot_sha256': _digest(receipt['snapshot']),
                               'answer_scope': SCOPE,
                               'not_stated_wording': 'Not stated in the selected retrieved excerpts.',
                               'semantic_collection_search_verified': False,
                               'retrieval': deepcopy(receipt['retrieval']),
                               'packing': deepcopy(receipt['packing'])}}
    except (KeyError, TypeError) as exc:
        raise ValueError('Invalid collection workflow receipt') from exc
