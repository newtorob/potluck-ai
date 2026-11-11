"""Scoped retrieval from local indexed snapshots; caller supplies the selection scope.

Workspace and collection IDs select evidence; they do not authenticate the caller.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from potluck_local.services.memory_db import get_memory_db_path

MAX_SNAPSHOT_AGE = timedelta(hours=24)
MAX_CANDIDATES = 200
MAX_CLAIM_SOURCES = 16
MAX_QUERY_LENGTH = 1000
MAX_TERMS = 20
MAX_COLLECTIONS = 100
_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_STOP = {'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'did', 'do', 'does',
         'for', 'from', 'how', 'in', 'is', 'it', 'of', 'on', 'or', 'the', 'to',
         'was', 'were', 'what', 'when', 'where', 'which', 'who', 'why', 'with'}


@dataclass(frozen=True)
class EvidenceScope:
    workspace: str
    collection_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.workspace, str) or not self.workspace.strip()
                or self.workspace != self.workspace.strip()
                or not isinstance(self.collection_ids, tuple) or not self.collection_ids
                or len(self.collection_ids) > MAX_COLLECTIONS
                or any(not isinstance(x, str) or not _ID.fullmatch(x) for x in self.collection_ids)
                or len(set(self.collection_ids)) != len(self.collection_ids)):
            raise ValueError('An explicit workspace and unique collection IDs are required')


def _freshness(timestamp: str | None, now: datetime | None = None) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp or '')
        if parsed.tzinfo is None:
            return 'unknown'
        age = (now or datetime.now(timezone.utc)) - parsed
        return 'recent' if timedelta(0) <= age <= MAX_SNAPSHOT_AGE else 'stale'
    except (ValueError, OverflowError):
        return 'unknown'


async def _ready(db: aiosqlite.Connection, scope: EvidenceScope) -> tuple[dict[str, str], int]:
    if not isinstance(scope, EvidenceScope):
        raise ValueError('Explicit EvidenceScope required')
    ph = ','.join('?' for _ in scope.collection_ids)
    cur = await db.execute(
        f"SELECT id,last_scan_at FROM source_collections WHERE workspace=? AND id IN ({ph}) "
        "AND scan_status='ready'", (scope.workspace, *scope.collection_ids))
    rows = await cur.fetchall()
    now = datetime.now(timezone.utc)
    ready = {r['id']: r['last_scan_at'] for r in rows
             if _freshness(r['last_scan_at'], now) == 'recent'}
    return ready, len(rows) - len(ready)


def _source(row: aiosqlite.Row, ready: dict[str, str]) -> dict[str, Any]:
    item = dict(row)
    item.update(kind='source_chunk', last_scan_at=ready[item['collection_id']],
                freshness='recent', uncertainty='Source text has not been reviewed.')
    item['citation_id'] = f"source:{item['chunk_id']}:{item['revision_id']}"
    return item


_SOURCE_SELECT = ('SELECT c.id collection_id, d.id document_id, d.revision_id, '
                  'd.relative_path, d.updated_at, k.id chunk_id, k.anchor, k.heading, k.content '
                  'FROM source_collections c JOIN source_documents d ON d.collection_id=c.id '
                  'JOIN source_chunks k ON k.document_id=d.id ')
_CLAIM_SELECT = ('SELECT id, claim_key, content, reviewer, reason, created_at, reviewed_at '
                 'FROM reviewed_claims ')


async def _claim(db: aiosqlite.Connection, row: aiosqlite.Row,
                 ready: dict[str, str], workspace: str) -> dict[str, Any] | None:
    claim = dict(row)
    cur = await db.execute(
        'SELECT collection_id,document_id,revision_id,chunk_id FROM reviewed_claim_sources '
        'WHERE claim_id=? ORDER BY collection_id,document_id,chunk_id LIMIT ?',
        (claim['id'], MAX_CLAIM_SOURCES + 1))
    refs = await cur.fetchall()
    if not refs or len(refs) > MAX_CLAIM_SOURCES:
        return None
    sources = []
    for ref in refs:
        if ref['collection_id'] not in ready:
            return None
        cur = await db.execute(
            _SOURCE_SELECT + 'WHERE c.workspace=? AND c.id=? AND d.id=? '
            'AND d.revision_id=? AND k.id=?',
            (workspace, ref['collection_id'], ref['document_id'], ref['revision_id'],
             ref['chunk_id']))
        source = await cur.fetchone()
        if source is None:
            return None
        sources.append(_source(source, ready))
    claim.update(kind='reviewed_claim', status='approved', sources=sources,
                 citation_id=f"claim:{claim['id']}",
                 uncertainty='Human review is recorded; entailment has not been checked.')
    return claim


def _terms(query: str) -> set[str]:
    terms = set(re.findall(r'[^\W_]+', query.casefold())) - _STOP
    if len(terms) > MAX_TERMS:
        raise ValueError('Query has too many distinct terms')
    return terms


def _score(item: dict[str, Any], terms: set[str]) -> int:
    fields = ('content', 'heading', 'relative_path', 'claim_key')
    text = ' '.join(str(item.get(x, '')) for x in fields)
    return len(terms & set(re.findall(r'[^\W_]+', text.casefold())))


async def lookup_evidence(
    scope: EvidenceScope, query: str, max_results: int = 10, *,
    retrieval_version: str = 'legacy',
) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
        raise ValueError('A non-empty query of at most 1000 characters is required')
    if (isinstance(max_results, bool) or not isinstance(max_results, int)
            or not 1 <= max_results <= 100):
        raise ValueError('max_results must be between 1 and 100')
    if retrieval_version not in ('legacy', 'v2'):
        raise ValueError('Unsupported retrieval version')
    terms = _terms(query)
    candidates: list[dict[str, Any]] = []
    capped = False
    async with aiosqlite.connect(get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await db.create_function('unicode_casefold', 1, str.casefold, deterministic=True)
        await db.execute('BEGIN')
        ready, stale = await _ready(db, scope)
        collection_names: dict[str, str] = {}
        if retrieval_version == 'v2' and ready:
            ph = ','.join('?' for _ in ready)
            cur = await db.execute(f'SELECT id,name FROM source_collections WHERE id IN ({ph})', tuple(ready))
            collection_names = {row['id']: row['name'] for row in await cur.fetchall()}
        if ready and terms:
            ph = ','.join('?' for _ in ready)
            ordered = sorted(terms)
            source_field = (
                "unicode_casefold(k.content || ' ' || k.heading || ' ' || d.relative_path)"
            )
            source_match = ' OR '.join(f"instr({source_field},?)>0" for _ in ordered)
            cur = await db.execute(
                _SOURCE_SELECT + f'WHERE c.workspace=? AND c.id IN ({ph}) AND ({source_match}) '
                + ('ORDER BY c.name,d.relative_path,k.ordinal,k.content LIMIT ?'
                   if retrieval_version == 'v2' else 'ORDER BY c.id,d.relative_path,k.ordinal LIMIT ?'),
                (scope.workspace, *ready, *ordered, MAX_CANDIDATES + 1))
            rows = await cur.fetchall()
            capped |= len(rows) > MAX_CANDIDATES
            candidates.extend(_source(r, ready) for r in rows[:MAX_CANDIDATES])
            claim_match = ' OR '.join(
                "instr(unicode_casefold(content || ' ' || claim_key),?)>0" for _ in ordered)
            cur = await db.execute(
                _CLAIM_SELECT + f"WHERE workspace=? AND status='approved' AND ({claim_match}) "
                'AND EXISTS (SELECT 1 FROM reviewed_claim_sources r '
                'WHERE r.claim_id=reviewed_claims.id) '
                f'AND NOT EXISTS (SELECT 1 FROM reviewed_claim_sources r '
                f'WHERE r.claim_id=reviewed_claims.id AND r.collection_id NOT IN ({ph})) '
                + ('ORDER BY claim_key LIMIT ?' if retrieval_version == 'v2'
                   else 'ORDER BY claim_key,id LIMIT ?'),
                (scope.workspace, *ordered, *ready, MAX_CANDIDATES + 1))
            rows = await cur.fetchall()
            capped |= len(rows) > MAX_CANDIDATES
            for row in rows[:MAX_CANDIDATES]:
                claim = await _claim(db, row, ready, scope.workspace)
                if claim is not None:
                    if retrieval_version == 'v2':
                        claim['sources'].sort(key=lambda source: (
                            collection_names[source['collection_id']], source['relative_path'],
                            source['heading'], source['content']))
                    candidates.append(claim)
    ranked = [(item, _score(item, terms)) for item in candidates]
    ranked = [(item, score) for item, score in ranked if score]
    deprioritized = 0
    if retrieval_version == 'v2':
        def semantic_key(pair: tuple[dict[str, Any], int]) -> tuple[Any, ...]:
            item, score = pair
            source_keys = tuple((collection_names[s['collection_id']], s['relative_path'],
                                 s['heading'], s['content']) for s in item.get('sources', []))
            return (-score, item['kind'], collection_names.get(item.get('collection_id'), ''),
                    str(item.get('relative_path', '')), str(item.get('heading', '')),
                    str(item.get('claim_key', '')), str(item.get('content', '')), source_keys)
        ranked.sort(key=semantic_key)
        counts: dict[tuple[str, str], int] = {}
        preferred = []
        deferred = []
        for pair in ranked:
            item, _ = pair
            if item['kind'] == 'source_chunk':
                doc = (item['collection_id'], item['document_id'])
                counts[doc] = counts.get(doc, 0) + 1
                if counts[doc] > 2:
                    deferred.append(pair)
                    continue
            preferred.append(pair)
        deprioritized = len(deferred)
        eligible = preferred + deferred
    else:
        ranked.sort(key=lambda pair: (-pair[1], pair[0]['kind'], pair[0]['citation_id']))
        eligible = ranked
    selected = [item for item, _ in eligible[:max_results]]
    missing = []
    if len(ready) + stale < len(scope.collection_ids):
        missing.append('Some collections are unavailable or not ready; results are partial.')
    if stale:
        missing.append('Some requested snapshots are stale or have unknown age and need a rescan.')
    if not selected:
        missing.append('No matching evidence terms were found; this does not establish an answer.')
    if capped:
        missing.append('Candidate limit reached; matching evidence may be omitted.')
    if retrieval_version == 'v2' and len(eligible) > max_results:
        missing.append('Result limit reached; matching evidence was omitted.')
    result = {'workspace': scope.workspace, 'scope': {'collection_ids': list(scope.collection_ids)},
            'source_chunks': [x for x in selected if x['kind'] == 'source_chunk'],
            'reviewed_claims': [x for x in selected if x['kind'] == 'reviewed_claim'],
            'missing_evidence': missing,
            'limitations': ['Results reflect indexed snapshots; disk changes need a rescan.',
                            'Word overlap does not establish relevance or entailment.',
                            'At most 200 candidates per evidence kind are examined.'],
            'scope_status': {'requested': len(scope.collection_ids), 'ready': len(ready),
                             'unavailable': len(scope.collection_ids) - len(ready) - stale,
                             'needs_rescan': stale},
            'matching': 'case-insensitive whole-word overlap ranked by distinct shared terms',
            'truncated': capped or len(eligible) > max_results}
    if retrieval_version == 'v2':
        result['limitations'].append('The first two ranked chunks per document are preferred; additional chunks backfill remaining slots.')
        result['retrieval_version'] = 'v2'
        result['retrieval_trace'] = {'candidate_limit_reached': capped,
                                     'preferred_chunks_per_document': 2,
                                     'deprioritized_chunks': deprioritized,
                                     'result_omitted': max(0, len(eligible) - max_results),
                                     'result_limit_reached': len(eligible) > max_results}
    return result


async def resolve_citation(scope: EvidenceScope, citation_id: str) -> dict[str, Any] | None:
    if not isinstance(citation_id, str):
        return None
    parts = citation_id.split(':')
    if (len(parts) not in (2, 3) or parts[0] not in ('source', 'claim')
            or (parts[0] == 'source') != (len(parts) == 3)
            or any(not _ID.fullmatch(p) for p in parts[1:])):
        return None
    async with aiosqlite.connect(get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN')
        ready, _ = await _ready(db, scope)
        if not ready:
            return None
        if parts[0] == 'source':
            ph = ','.join('?' for _ in ready)
            cur = await db.execute(
                _SOURCE_SELECT + f'WHERE c.workspace=? AND c.id IN ({ph}) '
                'AND k.id=? AND d.revision_id=?', (scope.workspace, *ready, parts[1], parts[2]))
            row = await cur.fetchone()
            return _source(row, ready) if row else None
        cur = await db.execute(_CLAIM_SELECT + "WHERE workspace=? AND status='approved' AND id=?",
                               (scope.workspace, parts[1]))
        row = await cur.fetchone()
        return await _claim(db, row, ready, scope.workspace) if row else None
