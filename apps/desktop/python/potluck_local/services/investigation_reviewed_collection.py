"""Discover source documents and select only their currently approved claims."""
import aiosqlite

from .evidence_lookup import EvidenceScope, _CLAIM_SELECT, _SOURCE_SELECT, _claim, _ready, _score, _source, _terms
from .investigation_reviewed_evidence import MAX_CLAIMS, MAX_RECORD_CHARS, MAX_TOTAL_CHARS, _digest
from .memory_db import get_memory_db_path

VERSION = 'reviewed-collection-v1'
MAX_DOCUMENTS = 1000
MAX_DISCOVERY_CANDIDATES = 200


async def prepare_in_transaction(db, scope, query):
    if not isinstance(scope, EvidenceScope) or not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError('Invalid reviewed collection request')
    terms = _terms(query)
    if not terms:
        raise ValueError('No matching reviewed evidence')
    db.row_factory = aiosqlite.Row
    await db.create_function('unicode_casefold', 1, str.casefold, deterministic=True)
    ready, stale = await _ready(db, scope)
    if stale or set(ready) != set(scope.collection_ids):
        raise ValueError('Selected collections must be ready and recent')
    ph = ','.join('?' for _ in scope.collection_ids)
    cur = await db.execute(f'SELECT id,name,includes,excludes,scan_status,last_scan_at FROM source_collections WHERE workspace=? AND id IN ({ph}) ORDER BY id', (scope.workspace, *scope.collection_ids))
    collections = [dict(row) for row in await cur.fetchall()]
    cur = await db.execute(f'SELECT collection_id,id,relative_path,content_sha,revision_id,parser_version FROM source_documents WHERE collection_id IN ({ph}) ORDER BY collection_id,id LIMIT ?', (*scope.collection_ids, MAX_DOCUMENTS + 1))
    documents = [dict(row) for row in await cur.fetchall()]
    if len(documents) > MAX_DOCUMENTS:
        raise ValueError('Reviewed collection exceeds document limit')
    field = "unicode_casefold(k.content || ' ' || k.heading || ' ' || d.relative_path)"
    match = ' OR '.join(f'instr({field},?)>0' for _ in sorted(terms))
    cur = await db.execute(_SOURCE_SELECT + f'WHERE c.workspace=? AND c.id IN ({ph}) AND ({match}) ORDER BY c.name,d.relative_path,k.ordinal LIMIT ?', (scope.workspace, *scope.collection_ids, *sorted(terms), MAX_DISCOVERY_CANDIDATES + 1))
    rows = await cur.fetchall()
    if len(rows) > MAX_DISCOVERY_CANDIDATES:
        raise ValueError('Reviewed collection exceeds discovery limit')
    discovered = [_source(row, ready) for row in rows if _score(dict(row), terms)]
    document_ids = {row['document_id'] for row in discovered}
    cur = await db.execute(_CLAIM_SELECT + "WHERE workspace=? AND status='approved' "
        'AND EXISTS (SELECT 1 FROM reviewed_claim_sources r WHERE r.claim_id=reviewed_claims.id) '
        f'AND NOT EXISTS (SELECT 1 FROM reviewed_claim_sources r WHERE r.claim_id=reviewed_claims.id AND r.collection_id NOT IN ({ph})) '
        'ORDER BY claim_key,id LIMIT ?', (scope.workspace, *scope.collection_ids, MAX_CLAIMS + 1))
    rows = await cur.fetchall()
    if len(rows) > MAX_CLAIMS:
        raise ValueError('Reviewed evidence exceeds claim limit')
    all_claims = []
    for row in rows:
        item = await _claim(db, row, ready, scope.workspace)
        if item is None:
            raise ValueError('Reviewed evidence support is unavailable')
        all_claims.append(item)
    selected = [item for item in all_claims if _score(item, terms) or any(s['document_id'] in document_ids for s in item['sources'])]
    if not selected:
        raise ValueError('No matching reviewed evidence')
    sizes = [len(text) for claim in selected for text in [claim['content'], *(s['content'] for s in claim['sources'])]]
    if any(n > MAX_RECORD_CHARS for n in sizes) or sum(sizes) > MAX_TOTAL_CHARS:
        raise ValueError('Reviewed evidence exceeds presentation limit')
    trace = {'discovered': [{'collection_id': x['collection_id'], 'document_id': x['document_id'],
                             'revision_id': x['revision_id'], 'chunk_id': x['chunk_id'],
                             'citation_id': x['citation_id']} for x in discovered],
             'candidate_limit': MAX_DISCOVERY_CANDIDATES, 'document_limit': MAX_DOCUMENTS,
             'claim_limit': MAX_CLAIMS, 'selected_claims': len(selected)}
    body = {'version': VERSION, 'workspace': scope.workspace, 'collection_ids': list(scope.collection_ids),
            'query': query, 'collections': collections, 'documents': documents,
            'discovered': discovered, 'approved_claims': all_claims}
    result = {'workspace': scope.workspace, 'scope': {'collection_ids': list(scope.collection_ids)},
              'source_chunks': [], 'reviewed_claims': selected, 'missing_evidence': [],
              'limitations': ['Discovery uses lexical overlap in the indexed snapshot and is not semantically complete.',
                              'Only current approved claims and their support are evidence; human review is not entailment verification.'],
              'scope_status': {'requested': len(scope.collection_ids), 'ready': len(ready), 'unavailable': 0, 'needs_rescan': 0},
              'matching': 'case-insensitive whole-word overlap; discovery is not authority', 'truncated': False,
              'retrieval_version': VERSION, 'retrieval_trace': trace}
    return {'version': VERSION, 'snapshot_sha256': _digest(body), 'result': result}


async def prepare(scope, query):
    from .investigations import _privacy
    _privacy()
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await db.execute('BEGIN')
        return await prepare_in_transaction(db, scope, query)
