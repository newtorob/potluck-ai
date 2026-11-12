"""Read-only setup suggestions; literal text is not an authority decision."""
from __future__ import annotations

import re
from typing import Any

import aiosqlite

from . import claim_source_context, evidence_lookup as evidence
from .investigation_evidence_plan import bounded_query
from .memory_db import get_memory_db_path

VERSION = 'pilot-setup-drafts-v1'
MAX_DOCUMENTS = 3
_BOUNDARY = re.compile(r'(?<=\?)\s+(?=(?:what|when|where|which|who|why|how|can|could|does|do|did|is|are|will|should)\b)', re.I)


def question_parts(question: str) -> dict[str, Any]:
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        raise ValueError('Enter a question of at most 500 characters')
    # Quoted punctuation is ambiguous; keep the whole request instead of guessing.
    parts = [question.strip()] if any(c in question for c in ('"', "'", '`', '“', '”')) else _BOUNDARY.split(question.strip())
    if len(parts) > 6 or len({' '.join(p.split()).casefold() for p in parts}) != len(parts):
        parts = [question.strip()]
    return {'version': VERSION, 'question': question, 'parts': parts,
            'coverage': 'not_verified', 'origin': 'literal_question_sentences',
            'limitations': ['Only explicit question sentences are separated. Review compound requests and shared conditions before confirming.']}


async def source_drafts(scope: evidence.EvidenceScope, question: str) -> dict[str, Any]:
    parts = question_parts(question)
    query, trace = bounded_query(question)
    result = await evidence.lookup_evidence(scope, query, max_results=100, retrieval_version='v2')
    if result['scope_status']['ready'] != len(scope.collection_ids):
        raise ValueError('All selected collections must be ready and recent')
    hits: dict[tuple[str, str], dict[str, Any]] = {}
    for item in result['source_chunks']:
        hits.setdefault((item['collection_id'], item['document_id']), item)
    drafts = []
    async with aiosqlite.connect(get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN')
        ready, _ = await evidence._ready(db, scope)
        if len(ready) != len(scope.collection_ids):
            raise ValueError('All selected collections must be ready and recent')
        for (collection_id, document_id), hit in list(hits.items())[:MAX_DOCUMENTS]:
            # Read bounded metadata/content so a large source cannot balloon the response.
            cur = await db.execute(
                'SELECT k.id, length(k.content) size FROM source_chunks k '
                'JOIN source_documents d ON k.document_id=d.id '
                'JOIN source_collections c ON d.collection_id=c.id '
                'WHERE c.workspace=? AND c.id=? AND d.id=? AND d.revision_id=? '
                'ORDER BY k.ordinal LIMIT ?',
                (scope.workspace, collection_id, document_id, hit['revision_id'],
                 claim_source_context.MAX_CITATIONS + 1))
            rows = await cur.fetchall()
            draft = {'relative_path': hit['relative_path'], 'collection_id': collection_id,
                     'document_id': document_id, 'revision_id': hit['revision_id'],
                     'matched_citation_id': hit['citation_id'], 'preview': None, 'notice': ''}
            if not rows:
                raise ValueError('Source citation unavailable')
            if (len(rows) > claim_source_context.MAX_CITATIONS
                    or sum(row['size'] for row in rows) > claim_source_context.MAX_CONTENT_CHARS):
                draft['notice'] = 'This document exceeds the complete-draft limit. Search and review its excerpts manually.'
            else:
                draft['citation_ids'] = [f"source:{row['id']}:{hit['revision_id']}" for row in rows]
            drafts.append(draft)
    for draft in drafts:
        citations = draft.pop('citation_ids', None)
        if citations is not None:
            try:
                draft['preview'] = await claim_source_context.preview(scope, citations)
            except ValueError as exc:
                if str(exc) != claim_source_context.LIMIT_ERROR:
                    raise
                draft['notice'] = 'This document exceeds the complete-draft limit. Search and review its excerpts manually.'
    return {'version': VERSION, 'question': question, 'query_trace': trace,
            'question_parts': parts, 'candidates': drafts,
            'truncated': result['truncated'] or len(hits) > MAX_DOCUMENTS,
            'matching_documents_seen': len(hits),
            'limitations': [
                'Suggestions use word overlap, not verified relevance or complete corpus coverage.',
                'Prepared candidates include every indexed chunk of that document, not unindexed content.',
                'Review dates, draft status, conditions and exceptions before saving or approving.',
                *result['missing_evidence'],
            ]}
