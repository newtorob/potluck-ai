"""Opt-in, bounded selection of currently approved claims with current support."""
from copy import deepcopy
import hashlib
import json

import aiosqlite

from .evidence_lookup import EvidenceScope, _CLAIM_SELECT, _claim, _ready, _score, _terms
from .memory_db import get_memory_db_path

VERSION = 'reviewed-current-v1'
MAX_CLAIMS = 100
MAX_RECORD_CHARS = 6000
MAX_TOTAL_CHARS = 24000


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


async def prepare_in_transaction(db, scope, query):
    """Read the full matching set in the caller's transaction, or fail closed."""
    if not isinstance(scope, EvidenceScope) or not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError('Invalid reviewed evidence request')
    terms = _terms(query)
    if not terms:
        raise ValueError('No matching reviewed evidence')
    db.row_factory = aiosqlite.Row
    await db.create_function('unicode_casefold', 1, str.casefold, deterministic=True)
    ready, stale = await _ready(db, scope)
    if stale or set(ready) != set(scope.collection_ids):
        raise ValueError('Selected collections must be ready and recent')
    ph = ','.join('?' for _ in scope.collection_ids)
    match = ' OR '.join("instr(unicode_casefold(content || ' ' || claim_key),?)>0" for _ in sorted(terms))
    cur = await db.execute(
        _CLAIM_SELECT + f"WHERE workspace=? AND status='approved' AND ({match}) "
        'AND EXISTS (SELECT 1 FROM reviewed_claim_sources r WHERE r.claim_id=reviewed_claims.id) '
        f'AND NOT EXISTS (SELECT 1 FROM reviewed_claim_sources r WHERE r.claim_id=reviewed_claims.id AND r.collection_id NOT IN ({ph})) '
        'ORDER BY claim_key,id LIMIT ?',
        (scope.workspace, *sorted(terms), *scope.collection_ids, MAX_CLAIMS + 1))
    rows = await cur.fetchall()
    if len(rows) > MAX_CLAIMS:
        raise ValueError('Reviewed evidence exceeds claim limit')
    claims = []
    for row in rows:
        if not _score(dict(row), terms):
            continue
        claim = await _claim(db, row, ready, scope.workspace)
        if claim is None:
            raise ValueError('Reviewed evidence support is unavailable')
        claims.append(claim)
    if not claims:
        raise ValueError('No matching reviewed evidence')
    claims.sort(key=lambda item: (-_score(item, terms), item['claim_key'], item['id']))
    sizes = [len(text) for claim in claims for text in [claim['content'], *(s['content'] for s in claim['sources'])]]
    if any(size > MAX_RECORD_CHARS for size in sizes) or sum(sizes) > MAX_TOTAL_CHARS:
        raise ValueError('Reviewed evidence exceeds presentation limit')
    cur = await db.execute(f'SELECT id,name,includes,excludes,scan_status,last_scan_at FROM source_collections WHERE workspace=? AND id IN ({ph}) ORDER BY id', (scope.workspace, *scope.collection_ids))
    collections = [dict(row) for row in await cur.fetchall()]
    body = {'version': VERSION, 'workspace': scope.workspace, 'collection_ids': list(scope.collection_ids),
            'query': query, 'collections': collections, 'claims': claims}
    result = {'workspace': scope.workspace, 'scope': {'collection_ids': list(scope.collection_ids)},
              'source_chunks': [], 'reviewed_claims': claims, 'missing_evidence': [],
              'limitations': ['Only matching currently approved claims with resolved in-scope support are included.',
                              'Human review is recorded; entailment is not verified.'],
              'scope_status': {'requested': len(scope.collection_ids), 'ready': len(ready), 'unavailable': 0, 'needs_rescan': 0},
              'matching': 'case-insensitive whole-word overlap ranked by distinct shared terms', 'truncated': False,
              'retrieval_version': VERSION, 'retrieval_trace': {'claim_limit': MAX_CLAIMS, 'matched_claims': len(claims), 'result_limit_reached': False}}
    return {'version': VERSION, 'snapshot_sha256': _digest(body), 'result': result}


async def prepare(scope, query):
    from .investigations import _privacy
    _privacy()
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await db.execute('BEGIN')
        return await prepare_in_transaction(db, scope, query)


async def verify(scope, query, receipt):
    """Reject changed or caller-mutated receipts against current indexed evidence."""
    if await prepare(scope, query) != receipt:
        raise ValueError('Reviewed evidence changed')


def flattened(evidence):
    """Present each reviewed claim and its supporting documents as separate records."""
    records = []
    seen = set()
    for claim in evidence:
        if claim.get('kind') != 'reviewed_claim':
            raise ValueError('Expected reviewed evidence')
        for record in [claim, *claim['sources']]:
            citation = record['citation_id']
            if citation in seen:
                continue
            seen.add(citation)
            flat = {'kind': 'source_chunk', 'citation_id': citation,
                            'content': record['content'],
                            'presented_content': record.get('presented_content', record['content']),
                            'relative_path': record.get('relative_path', ''),
                    'heading': record.get('heading', '')}
            if record is not claim and 'structural_boundary' in record:
                flat['structural_boundary'] = deepcopy(record['structural_boundary'])
            records.append(flat)
    return records


def snippets(evidence):
    from . import investigation_context
    catalog, trace = investigation_context.snippets_from_evidence(flattened(evidence), version='paragraph-v3')
    trace['version'] = 'reviewed-paragraph-v1'
    return catalog, trace


def prompt_links(evidence, catalog):
    ids = {}
    for item in catalog:
        ids.setdefault(item['citation_id'], []).append(item['snippet_id'])
    return [{'claim_snippet_ids': ids[claim['citation_id']],
             'support_snippet_ids': [sid for source in claim['sources'] for sid in ids[source['citation_id']]]}
            for claim in evidence]
