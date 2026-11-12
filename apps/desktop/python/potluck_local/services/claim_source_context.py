"""Bounded, explicitly requested previews of indexed source context."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import aiosqlite

from potluck_local.services import evidence_lookup as evidence
from potluck_local.services.memory_db import get_memory_db_path

MAX_CITATIONS = 16
MAX_OPENING_CHUNKS = 4
MAX_OPENING_CHARS = 6000
MAX_CONTENT_CHARS = 16000
LIMIT_ERROR = 'Source context exceeds the preview limit; select fewer or shorter excerpts and review the full source.'


async def preview(scope: evidence.EvidenceScope, citation_ids: list[str]) -> dict[str, Any]:
    """Keep indexed context and citations together without approving their meaning."""
    if (not isinstance(citation_ids, list) or not 1 <= len(citation_ids) <= MAX_CITATIONS
            or any(not isinstance(c, str) for c in citation_ids)
            or len(set(citation_ids)) != len(citation_ids)):
        raise ValueError('Choose one to sixteen distinct source citations')
    selected = []
    async with aiosqlite.connect(get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN')
        ready, _ = await evidence._ready(db, scope)
        if len(ready) != len(scope.collection_ids):
            raise ValueError('All selected collections must be ready and recent')
        bounded = evidence._SOURCE_SELECT.replace('k.content ', 'substr(k.content,1,16001) content ')
        bounded = bounded.replace('c.id collection_id,', 'c.id collection_id, c.name collection_name,')
        for citation in citation_ids:
            parts = citation.split(':')
            if (len(parts) != 3 or parts[0] != 'source'
                    or any(not evidence._ID.fullmatch(p) for p in parts[1:])):
                raise ValueError('Source citation unavailable')
            ph = ','.join('?' for _ in ready)
            cur = await db.execute(
                bounded + f'WHERE c.workspace=? AND c.id IN ({ph}) AND k.id=? AND d.revision_id=?',
                (scope.workspace, *ready, parts[1], parts[2]))
            row = await cur.fetchone()
            if row is None:
                raise ValueError('Source citation unavailable')
            item = evidence._source(row, ready)
            if len(item['content']) > MAX_CONTENT_CHARS:
                raise ValueError(LIMIT_ERROR)
            selected.append(item)

        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for item in selected:
            key = (item['collection_id'], item['document_id'])
            if key not in groups:
                cur = await db.execute(
                    bounded + 'WHERE c.workspace=? AND c.id=? AND d.id=? AND d.revision_id=? '
                    'ORDER BY k.ordinal LIMIT ?',
                    (scope.workspace, *key, item['revision_id'], MAX_OPENING_CHUNKS + 2))
                opening = []
                heading = None
                for row in await cur.fetchall():
                    if heading is not None and row['heading'] != heading:
                        break
                    if row['heading']:
                        heading = row['heading']
                    opening.append(evidence._source(row, ready))
                    if (len(opening) > MAX_OPENING_CHUNKS
                            or sum(len(s['heading']) + len(s['content']) for s in opening) > MAX_OPENING_CHARS):
                        raise ValueError(LIMIT_ERROR)
                if not opening:
                    raise ValueError('Source citation unavailable')
                groups[key] = {'relative_path': item['relative_path'],
                               'collection_name': item['collection_name'],
                               'collection_id': key[0], 'document_id': key[1],
                               'revision_id': item['revision_id'], 'opening': opening, 'excerpts': []}
            groups[key]['excerpts'].append(item)

        texts, expanded = [], []
        for group in groups.values():
            seen = set()
            blocks = []
            for source in group['opening'] + group['excerpts']:
                if source['citation_id'] in seen:
                    continue
                seen.add(source['citation_id'])
                expanded.append(source['citation_id'])
                blocks.append('\n\n'.join(v for v in (source['heading'], source['content']) if v))
            texts.append('Document: ' + group['collection_name'] + ' / ' + group['relative_path']
                         + '\n\n' + '\n\n'.join(blocks))
        content = '\n\n'.join(texts)
        if len(expanded) > MAX_CITATIONS or len(content) > MAX_CONTENT_CHARS:
            raise ValueError(LIMIT_ERROR)
        binding = json.dumps([scope.workspace, scope.collection_ids, expanded, content], ensure_ascii=False)
        return {'preview_id': hashlib.sha256(binding.encode()).hexdigest(),
                'content': content, 'citation_ids': expanded, 'documents': list(groups.values()),
                'limitations': [
                    'This is the indexed opening and selected excerpts, not automatic date or policy classification.',
                    'Review the full source for later conditions and exceptions. Disk changes require a rescan.',
                    'Adding source text to a draft does not approve it.',
                ]}
