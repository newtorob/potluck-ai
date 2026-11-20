"""Complete bounded reviewed-document input, independent of query ranking."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import aiosqlite

from .evidence_lookup import EvidenceScope, _CLAIM_SELECT, _claim, _ready
from .investigation_reviewed_collection_v5 import MAX_DOCUMENTS, MAX_SELECTED, MAX_CLAIM_CHARS
from .investigation_reviewed_evidence import MAX_CLAIMS, MAX_RECORD_CHARS, MAX_TOTAL_CHARS, _digest, snippets
from .memory_db import get_memory_db_path

VERSION = 'reviewed-collection-v7'
CONTRACT = 'complete-selected-documents-v1'
REFUSAL = 'Review every selected document in full, then rescan changed folders or select fewer documents. No answer was generated.'


class CoverageError(ValueError):
    pass


def _refuse(reason):
    raise CoverageError(reason + '. ' + REFUSAL)


async def inventory_in_transaction(db, scope):
    from .source_collections import _read_files
    db.row_factory = aiosqlite.Row
    ready, stale = await _ready(db, scope)
    if stale or set(ready) != set(scope.collection_ids):
        _refuse('All selected collections must be ready and recent')
    ph = ','.join('?' for _ in scope.collection_ids)
    cur = await db.execute(f'SELECT id,name,root_path,includes,excludes,scan_status,last_scan_at FROM source_collections WHERE workspace=? AND id IN ({ph}) ORDER BY id', (scope.workspace, *scope.collection_ids))
    collections = [dict(r) for r in await cur.fetchall()]
    cur = await db.execute(f'SELECT collection_id,id,relative_path,content_sha,revision_id,parser_version FROM source_documents WHERE collection_id IN ({ph}) ORDER BY collection_id,relative_path LIMIT ?', (*scope.collection_ids, MAX_DOCUMENTS + 1))
    documents = [dict(r) for r in await cur.fetchall()]
    if not documents or len(documents) > min(MAX_DOCUMENTS, MAX_SELECTED):
        _refuse('Selected documents exceed the complete evidence limit or are empty')
    for collection in collections:
        expected = {d['relative_path']: d['content_sha'] for d in documents if d['collection_id'] == collection['id']}
        current = {}
        try:
            for path, text in _read_files(Path(collection['root_path']), tuple(json.loads(collection['includes'])), tuple(json.loads(collection['excludes']))):
                current[path] = hashlib.sha256(text.encode()).hexdigest()
                if path not in expected or current[path] != expected[path]:
                    _refuse('Selected document files changed; rescan required')
                from .source_collections import PARSER_VERSION, HEADING_CONTEXT_PARSER_VERSION
                from .memory_safety import redact_credentials
                from .vault_ingestion import chunk_markdown
                doc = next(d for d in documents if d['collection_id'] == collection['id'] and d['relative_path'] == path)
                if doc['parser_version'] not in (PARSER_VERSION, HEADING_CONTEXT_PARSER_VERSION):
                    _refuse('Selected document parser is unavailable; rescan required')
                parsed = chunk_markdown(redact_credentials(text), preserve_headings=doc['parser_version'] == HEADING_CONTEXT_PARSER_VERSION)
                cur = await db.execute('SELECT anchor,heading,content FROM source_chunks WHERE document_id=? ORDER BY ordinal', (doc['id'],))
                indexed = [tuple(r) for r in await cur.fetchall()]
                if indexed != [(c.anchor, c.heading, c.body) for c in parsed]:
                    _refuse('Selected document indexed text changed; rescan required')
        except (OSError, UnicodeError, ValueError) as exc:
            if str(exc).endswith(REFUSAL):
                raise
            _refuse('Selected document files are unavailable; rescan required')
        if current != expected:
            _refuse('Selected document files changed; rescan required')
    return collections, documents


async def prepare_in_transaction(db, scope, query):
    if not isinstance(scope, EvidenceScope) or not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError('Invalid complete-document request')
    collections, documents = await inventory_in_transaction(db, scope)
    ready, _ = await _ready(db, scope)
    ph = ','.join('?' for _ in scope.collection_ids)
    cur = await db.execute(_CLAIM_SELECT + "WHERE workspace=? AND status='approved' "
        'AND EXISTS (SELECT 1 FROM reviewed_claim_sources r WHERE r.claim_id=reviewed_claims.id) '
        f'AND NOT EXISTS (SELECT 1 FROM reviewed_claim_sources r WHERE r.claim_id=reviewed_claims.id AND r.collection_id NOT IN ({ph})) '
        'ORDER BY claim_key,id LIMIT ?', (scope.workspace, *scope.collection_ids, MAX_CLAIMS + 1))
    rows = await cur.fetchall()
    if len(rows) > MAX_CLAIMS:
        _refuse('Reviewed evidence exceeds claim limit')
    candidates = []
    for row in rows:
        claim = await _claim(db, row, ready, scope.workspace)
        if claim is None:
            _refuse('Reviewed evidence support is unavailable')
        candidates.append(claim)
    selected, inventory = [], []
    for doc in documents:
        cur = await db.execute('SELECT id,ordinal,heading,content FROM source_chunks WHERE document_id=? ORDER BY ordinal', (doc['id'],))
        chunks = [dict(r) for r in await cur.fetchall()]
        if not chunks:
            _refuse('A selected document has no reviewable indexed text')
        name = next(c['name'] for c in collections if c['id'] == doc['collection_id'])
        blocks = ['\n\n'.join(v for v in ((k['content'],) if k['heading'] == k['content'] else (k['heading'], k['content'])) if v) for k in chunks]
        complete = 'Document: ' + name + ' / ' + doc['relative_path'] + '\n\n' + '\n\n'.join(blocks)
        identities = {(doc['collection_id'], doc['id'], doc['revision_id'], k['id']) for k in chunks}
        matches = [c for c in candidates if c['content'] == complete and
                   {(s['collection_id'], s['document_id'], s['revision_id'], s['chunk_id']) for s in c['sources']} == identities]
        if len(matches) != 1:
            _refuse('A selected document needs one complete approved source statement')
        claim = deepcopy(matches[0]); order = {k['id']: k['ordinal'] for k in chunks}
        claim['sources'].sort(key=lambda s: order[s['chunk_id']])
        selected.append(claim)
        inventory.append({**doc, 'claim_id': claim['id'], 'claim_citation_id': claim['citation_id'],
                          'claim_sha256': hashlib.sha256(complete.encode()).hexdigest(),
                          'chunks': [{'chunk_id': k['id'], 'ordinal': k['ordinal']} for k in chunks]})
    sizes = [len(x) for c in selected for x in [c['content'], *(s['content'] for s in c['sources'])]]
    if (any(n > MAX_RECORD_CHARS for n in sizes) or sum(sizes) > MAX_TOTAL_CHARS
            or sum(len(c['content']) for c in selected) > MAX_CLAIM_CHARS):
        _refuse('Complete selected evidence exceeds presentation limit')
    _, trace = snippets(selected)
    if trace['truncated']:
        _refuse('Complete selected evidence exceeds snippet limit')
    coverage = {'version': CONTRACT, 'selected_documents': len(documents),
                'supplied_documents': len(inventory), 'omitted_documents': 0,
                'documents': inventory,
                'scope': 'Current indexed Markdown documents in the selected folders, under their include/exclude and symlink rules.',
                'assessment': 'All complete approved document statements supplied; model interpretation and factual completeness still require review.'}
    trace = {'version': VERSION, 'selected_claim_ids': [c['id'] for c in selected],
             'omitted_claim_ids': [], 'omitted_count': 0, 'stop_reason': None,
             'document_coverage': coverage, 'snippet_count': len(snippets(selected)[0])}
    result = {'workspace': scope.workspace, 'scope': {'collection_ids': sorted(scope.collection_ids)},
              'source_chunks': [], 'reviewed_claims': selected, 'missing_evidence': [],
              'limitations': [coverage['scope'], coverage['assessment']],
              'scope_status': {'requested': len(scope.collection_ids), 'ready': len(ready), 'unavailable': 0, 'needs_rescan': 0},
              'matching': 'Complete selected reviewed documents; no query ranking or omitted prefix.',
              'truncated': False, 'retrieval_version': VERSION, 'retrieval_trace': trace}
    # Mount paths are host-local locations, not portable document identities.
    logical_collections = [{k: v for k, v in c.items() if k != 'root_path'} for c in collections]
    body = {'version': VERSION, 'workspace': scope.workspace, 'collection_ids': sorted(scope.collection_ids),
            'query': query, 'collections': logical_collections, 'result': result}
    return {'version': VERSION, 'snapshot_sha256': _digest(_logical(body)), 'result': result}


async def prepare(scope, query):
    from .investigations import _privacy
    _privacy()
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await db.execute('BEGIN')
        return await prepare_in_transaction(db, scope, query)


def _logical(value):
    # A successful unchanged rescan renews freshness without changing evidence.
    if isinstance(value, dict):
        return {k: _logical(v) for k, v in value.items() if k != 'last_scan_at'}
    if isinstance(value, list):
        return [_logical(v) for v in value]
    return value


async def persist_in_transaction(db, iid, receipt):
    await db.execute('CREATE TABLE IF NOT EXISTS investigation_document_coverage (investigation_id TEXT PRIMARY KEY, version TEXT NOT NULL, receipt TEXT NOT NULL)')
    await db.execute('INSERT INTO investigation_document_coverage VALUES (?,?,?)', (iid, VERSION, json.dumps({'binding_sha256': _digest(_logical(receipt))})))


def _contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _contains(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(_contains(a, e) for a, e in zip(actual, expected))
    return actual == expected


async def validate_in_transaction(db, scope, item):
    provenance = item.get('provenance') or {}; mode = (provenance.get('evidence_selection') or {}).get('version')
    bound = None
    if item.get('id'):
        exists = await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='investigation_document_coverage'")
        if await exists.fetchone():
            cur = await db.execute('SELECT version,receipt FROM investigation_document_coverage WHERE investigation_id=?', (item['id'],))
            bound = await cur.fetchone()
    if mode != VERSION:
        if bound or 'document_coverage' in provenance:
            raise CoverageError('Invalid document coverage mode')
        return
    try:
        receipt = await prepare_in_transaction(db, scope, provenance.get('retrieval_query'))
    except ValueError:
        raise CoverageError('Selected document coverage is no longer current') from None
    if item.get('id'):
        try:
            valid = (bound and bound[0] == VERSION and json.loads(bound[1]) ==
                     {'binding_sha256': _digest(_logical(receipt))})
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise CoverageError('Selected document coverage changed or is unavailable')
    if (provenance['evidence_selection'].get('snapshot_sha256') != receipt['snapshot_sha256']
            or provenance.get('document_coverage') != receipt['result']['retrieval_trace']['document_coverage']
            or not _contains(_logical(item['evidence']), _logical(receipt['result']['reviewed_claims']))):
        raise CoverageError('Invalid selected document coverage binding')
    for claim in item['evidence']:
        for record in [claim, *claim['sources']]:
            if record.get('presented_content', record['content']) != record['content']:
                raise CoverageError('Complete document input was truncated')
