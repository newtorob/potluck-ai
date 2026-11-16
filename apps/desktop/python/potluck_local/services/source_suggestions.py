"""Bounded document-topic matches for source review, never answer authority."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from typing import Any

import aiosqlite

from . import evidence_lookup as evidence
from .memory_db import get_memory_db_path

VERSION = 'document-topics-v1'
MAX_DOCUMENTS = 200
MAX_FEATURE_CHARS = 16000
MAX_FEATURE_CHUNKS = 16
_TOKEN = re.compile(r'[^\W_]+(?:-\d+)?')
_GENERIC = evidence._STOP | {
    'about', 'according', 'also', 'can', 'could', 'describe', 'doc', 'docs', 'document',
    'documents', 'explain', 'fix', 'fixes', 'fixing', 'give', 'given', 'gives', 'handle',
    'happen', 'happens', 'many', 'may', 'need', 'needed', 'needs', 'option', 'options',
    'please', 'propose', 'proposed', 'proposes', 'say', 'says', 'should', 'task', 'tasks',
    'tell', 'their', 'these', 'this', 'us', 'use', 'used', 'using', 'want', 'would', 's',
}
_CONVERSATIONAL = {'both', 'between', 'cause', 'cover', 'covers', 'differ', 'different',
                   'differs', 'its', 'just', 'still', 'without', 'work', 'works'}


def _normal(word: str) -> str:
    if len(word) > 4 and word.endswith('ies'):
        return word[:-3] + 'y'
    if len(word) > 3 and word.endswith('s') and not word.endswith(('ss', 'us', 'is', 'news')):
        return word[:-1]
    return word


def _words(text: str, *, abbreviations: bool = False) -> set[str]:
    text = unicodedata.normalize('NFKC', text)
    if abbreviations:
        # Standalone language names are equivalent; filename extensions are not.
        text = re.sub(r'(?i)(?<![\w.])js(?!\w)', 'JavaScript', text)
        text = re.sub(r'(?i)(?<![\w.])ts(?!\w)', 'TypeScript', text)
    return {_normal(t) for t in _TOKEN.findall(text.casefold())}


def _body(text: str, *, display_links: bool = False) -> str:
    opening = text.lstrip('\ufeff \n\t')
    if opening.startswith('---\n'):
        closing = re.search(r'(?m)^---[ \t]*$', opening[4:])
        if closing:
            metadata = opening[4:4 + closing.start()]
            first = next((line for line in metadata.splitlines() if line.strip()), '')
            if re.match(r'^[A-Za-z_][\w-]*\s*:', first):
                text = opening[4 + closing.end():]
    # Only the wiki-link footer is ignored; ordinary Related prose stays searchable.
    text = re.sub(r'(?im)^[ \t]*Related:[ \t]*(?:\[\[[^\]\n]+\]\][^\w\n]*)+$', '', text)
    if display_links:
        text = re.sub(r'\[\[([^\]\n]+)\]\]', lambda m: m[1].split('|', 1)[-1], text)
    return text


async def rank(scope: evidence.EvidenceScope, query: str, *, version: str = VERSION) -> dict[str, Any]:
    if version not in (VERSION, 'document-topics-v2', 'document-topics-v3', 'document-topics-v4'):
        raise ValueError('Unsupported source suggestion version')
    selective = version != VERSION
    display = version == 'document-topics-v4'
    generic = {_normal(t) for t in _GENERIC | (_CONVERSATIONAL if selective else set())}
    terms = _words(query, abbreviations=display) - generic
    documents = []
    incomplete = False
    maximum_read = 0
    async with aiosqlite.connect(get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN')
        ready, _ = await evidence._ready(db, scope)
        if len(ready) != len(scope.collection_ids):
            raise ValueError('All selected collections must be ready and recent')
        placeholders = ','.join('?' for _ in scope.collection_ids)
        cursor = await db.execute(
            'SELECT c.id collection_id,c.name collection_name,d.id document_id,'
            'd.relative_path,d.revision_id FROM source_documents d '
            'JOIN source_collections c ON d.collection_id=c.id '
            f'WHERE c.workspace=? AND c.id IN ({placeholders}) '
            'ORDER BY c.name,d.relative_path,d.id LIMIT ?',
            (scope.workspace, *scope.collection_ids, MAX_DOCUMENTS + 1))
        rows = await cursor.fetchall()
        incomplete |= len(rows) > MAX_DOCUMENTS
        for row in rows[:MAX_DOCUMENTS]:
            doc = dict(row)
            cursor = await db.execute(
                'WITH bounded AS (SELECT heading,content,ordinal FROM source_chunks '
                'WHERE document_id=? ORDER BY ordinal LIMIT ?), budgeted AS ('
                'SELECT *,coalesce(sum(min(length(heading),512)+length(content)+2) '
                'OVER (ORDER BY ordinal ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0) '
                'prior_chars FROM bounded) '
                'SELECT substr(heading,1,min(512,max(0,?-prior_chars))) heading,'
                'substr(content,1,max(0,?-prior_chars-min(length(heading),512)-2)) content,'
                'length(content) size,length(heading) heading_size,prior_chars '
                'FROM budgeted ORDER BY ordinal',
                (doc['document_id'], MAX_FEATURE_CHUNKS + 1, MAX_FEATURE_CHARS, MAX_FEATURE_CHARS))
            chunks = await cursor.fetchall()
            if not chunks:
                continue
            maximum_read = max(maximum_read, sum(len(c['heading']) + len(c['content']) for c in chunks))
            text = ''.join(c['heading'] + '\n' + c['content'] + '\n' for c in chunks[:MAX_FEATURE_CHUNKS])
            incomplete |= len(chunks) > MAX_FEATURE_CHUNKS or any(
                c['heading_size'] > 512 or c['prior_chars'] + min(c['heading_size'], 512) + c['size'] + 2
                > MAX_FEATURE_CHARS for c in chunks[:MAX_FEATURE_CHUNKS])
            body = _words(_body(text[:MAX_FEATURE_CHARS], display_links=display), abbreviations=display)
            title = _words(doc['relative_path'].rsplit('/', 1)[-1].rsplit('.', 1)[0], abbreviations=display)
            headings = ' '.join(c['heading'] for c in chunks[:MAX_FEATURE_CHUNKS])
            title |= _words(_body(headings, display_links=True) if display else headings, abbreviations=display)
            documents.append((doc, title, body))
    frequencies = Counter(t for _, title, body in documents for t in (title | body) & terms)
    distinct = {t for t, count in frequencies.items() if count <= max(1, len(documents) // 2)}
    ranked = []
    for doc, title, body in documents:
        matched = (title | body) & terms
        if selective and len(terms) >= 3 and len(matched) < 2:
            continue
        # A common word alone should not fill another review card. Two title-led
        # topic matches remain eligible for questions that span several topics.
        if not matched or (distinct and not matched & distinct
                           and not (len(matched) >= 2 and matched & title)):
            continue
        weights = {t: 1 + math.log((len(documents) + 1) / (frequencies[t] + 1)) for t in matched}
        score = sum(weights[t] * (2 if t in title else 1) for t in matched)
        doc['match'] = {'version': version, 'title_terms': sorted(matched & title),
                        'text_terms': sorted(matched - title)}
        ranked.append((score, doc))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]['collection_name'],
                                 pair[1]['relative_path'], pair[1]['document_id']))
    clauses = [_words(part, abbreviations=display) - generic for part in re.split(r'(?i)\band\b|\?|;', query)]
    clauses = [part for part in clauses if part]
    if display and sum(len(part) >= 2 for part in clauses) >= 2 and ranked:
        chosen = set()
        for part in clauses:
            scores = []
            for _, doc in ranked:
                match = doc['match']
                title = set(match['title_terms'])
                words = (title | set(match['text_terms'])) & part
                if len(words) < min(2, len(part)):
                    continue
                score = sum((1 + math.log((len(documents) + 1) / (frequencies[t] + 1)))
                            * (2 if t in title else 1) for t in words)
                scores.append((score, len(words), doc['document_id']))
            if scores:
                best = max(scores, key=lambda row: (row[0], row[1]))
                chosen.update(did for score, count, did in scores
                              if score >= best[0] * .9 and count >= best[1])
        ranked = [(score, doc) for score, doc in ranked if doc['document_id'] in chosen]
    elif selective and ranked:
        selected = [ranked[0]]
        first = ranked[0][1]['match']
        covered = set(first['title_terms']) | set(first['text_terms'])
        first_word_count = len(covered)
        for score, doc in ranked[1:]:
            match = doc['match']
            words = set(match['title_terms']) | set(match['text_terms'])
            novel = words - covered
            # Keep competing strong sources even when their words overlap.
            # Weak extras need to contribute another requested topic instead.
            if version in ('document-topics-v3', 'document-topics-v4'):
                adds_clause = len(clauses) > 1 and any(
                    len(part & covered) < min(2, len(part))
                    and len(part & words) >= min(2, len(part)) for part in clauses)
                keep = (score >= ranked[0][0] * .9 and len(words) >= first_word_count) or adds_clause
            else:
                keep = (score >= ranked[0][0] * .75 or len(novel) >= 2
                        or bool(novel & set(match['title_terms'])))
            if keep:
                selected.append((score, doc))
                covered |= words
        ranked = selected
    return {'documents': [doc for _, doc in ranked], 'incomplete': incomplete,
            'collection_scans': ready, 'maximum_document_feature_chars_read': maximum_read,
            'documents_considered': len(documents), 'topic_terms': sorted(terms),
            'ignored_terms': sorted(_words(query) & generic)}


async def validate_current(scope: evidence.EvidenceScope, drafts: list[dict[str, Any]],
                           collection_scans: dict[str, str]) -> None:
    async with aiosqlite.connect(get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN')
        ready, _ = await evidence._ready(db, scope)
        if len(ready) != len(scope.collection_ids) or ready != collection_scans:
            raise ValueError('All selected collections must be ready and recent')
        for draft in drafts:
            cursor = await db.execute(
                'SELECT 1 FROM source_documents d JOIN source_collections c ON d.collection_id=c.id '
                'WHERE c.workspace=? AND c.id=? AND d.id=? AND d.revision_id=?',
                (scope.workspace, draft['collection_id'], draft['document_id'], draft['revision_id']))
            if draft['collection_id'] not in scope.collection_ids or await cursor.fetchone() is None:
                raise ValueError('Source citation unavailable')
