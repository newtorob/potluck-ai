"""Rank current approved claims using conservative word-form matches to their bound evidence."""
import aiosqlite
import math
import re

from .investigation_reviewed_collection_v2 import FUNCTION_WORDS

from .evidence_lookup import EvidenceScope, _CLAIM_SELECT, _SOURCE_SELECT, _claim, _ready, _score, _source, _terms
from .investigation_reviewed_evidence import MAX_CLAIMS, MAX_RECORD_CHARS, MAX_TOTAL_CHARS, _digest
from .memory_db import get_memory_db_path

VERSION = 'reviewed-collection-v4'
MAX_SELECTED = 6
MAX_CLAIM_CHARS = 6000
RANKING_FUNCTION_WORDS = FUNCTION_WORDS | frozenset({'after','before','during','until','since','while','under','over','about','into','through','between','within','without'})
MAX_DOCUMENTS = 1000
MAX_DISCOVERY_CANDIDATES = 200


def _word_form(word):
    # Small, deterministic inflection handling; preserve short and non-ASCII tokens.
    if not word.isascii() or not word.isalpha() or len(word) < 5: return word
    if word.endswith('ies') and len(word) > 5: return word[:-3] + 'y'
    if word.endswith('ing') and len(word) > 6: return word[:-3]
    if word.endswith('ed') and len(word) > 5: return word[:-2]
    if word.endswith('s') and not word.endswith(('ss','us','is')): return word[:-1]
    return word


def _tokens(value):
    return {_word_form(word) for word in re.findall(r'[^\W_]+', str(value).casefold())}


async def prepare_in_transaction(db, scope, query):
    if not isinstance(scope, EvidenceScope) or not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError('Invalid reviewed collection request')
    original_terms = _terms(query)
    terms = {_word_form(t) for t in original_terms - RANKING_FUNCTION_WORDS}
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
    # IDF is computed over the complete approved in-scope inventory, not discovered documents.
    # Match exact lexical words in the claim, its bound source headings and chunks.
    tokenize = _tokens
    fields = []
    for item in all_claims:
        direct = tokenize(item['content'])
        heading = set().union(*(tokenize(src['heading']) for src in item['sources']))
        support = set().union(*(tokenize(src['content']) for src in item['sources']))
        fields.append((direct, heading, support))
    frequencies = {term: sum(term in (a | b | c) for a,b,c in fields) for term in terms}
    weights = {term: math.log((len(all_claims)+1)/(frequencies[term]+1))+1 for term in terms}
    ranked = []
    for item,(direct,heading,support) in zip(all_claims,fields):
        shared = terms & (direct | heading | support)
        if not shared:
            continue
        score = round(sum(weights[t] * (5 * (t in direct) + 2 * (t in heading) + 0.25 * (t in support)) for t in sorted(shared)), 12)
        ranked.append((score,item,sorted(shared)))
    ranked.sort(key=lambda row: (-row[0],row[1]['claim_key'],row[1]['id']))
    if not ranked:
        raise ValueError('No matching reviewed evidence')
    selected=[]; claim_chars=0; total_chars=0; stopped=None
    for score,item,shared in ranked:
        sizes=[len(item['content']),*(len(src['content']) for src in item['sources'])]
        reason = ('record_limit' if any(n > MAX_RECORD_CHARS for n in sizes) else
                  'claim_count' if len(selected) >= MAX_SELECTED else
                  'claim_characters' if claim_chars + sizes[0] > MAX_CLAIM_CHARS else
                  'presentation_characters' if total_chars + sum(sizes) > MAX_TOTAL_CHARS else None)
        if reason is not None:
            if not selected:
                raise ValueError('Highest ranked reviewed evidence exceeds presentation limit')
            stopped=reason
            break
        selected.append(item); claim_chars+=sizes[0];total_chars+=sum(sizes)
    omitted=ranked[len(selected):]
    ranking=[{'claim_id':item['id'],'score':round(score,12),'matched_terms':shared} for score,item,shared in ranked]
    trace={'discovery_matching':'Normalized query terms against raw source whole words; discovery does not normalize source words.',
           'discovered': [{'collection_id': x['collection_id'], 'document_id': x['document_id'],
                           'revision_id': x['revision_id'], 'chunk_id': x['chunk_id'],
                           'citation_id': x['citation_id']} for x in discovered],
           'candidate_limit': MAX_DISCOVERY_CANDIDATES, 'document_limit': MAX_DOCUMENTS,
           'claim_limit': MAX_CLAIMS, 'selected_limit':MAX_SELECTED,'claim_character_limit':MAX_CLAIM_CHARS,
           'query_terms':{'version':'inflection-filter-v1','retained':sorted(terms),'ignored_function_words':sorted(original_terms & RANKING_FUNCTION_WORDS)},
           'ranking':ranking,'selected_claim_ids':[x['id'] for x in selected],
           'omitted_claim_ids':[item['id'] for _,item,_ in omitted], 'omitted_count':len(omitted),
           'unmatched_claim_ids':[item['id'] for item,field in zip(all_claims,fields) if not terms & (field[0] | field[1] | field[2])],
           'stop_reason':stopped,'claim_characters':claim_chars,'presentation_characters':total_chars}
    body={'version':VERSION,'workspace':scope.workspace,'collection_ids':list(scope.collection_ids),
          'query':query,'collections':collections,'documents':documents,'discovered':discovered,
          'approved_claims':all_claims,'ranking':trace}
    result={'workspace':scope.workspace,'scope':{'collection_ids':list(scope.collection_ids)},
            'source_chunks':[],'reviewed_claims':selected,'missing_evidence':[],
            'limitations':['Selection is bounded lexical ranking, not semantic completeness or evidence of absence.',
                           'Only current approved claims and their support are evidence; human review is not entailment verification.'],
            'scope_status':{'requested':len(scope.collection_ids),'ready':len(ready),'unavailable':0,'needs_rescan':0},
            'matching':'normalized word-form IDF over claim and bound source heading/chunk; selected prefix only',
            'truncated':bool(omitted),'retrieval_version':VERSION,'retrieval_trace':trace}
    return {'version': VERSION, 'snapshot_sha256': _digest(body), 'result': result}


async def prepare(scope, query):
    from .investigations import _privacy
    _privacy()
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await db.execute('BEGIN')
        return await prepare_in_transaction(db, scope, query)
