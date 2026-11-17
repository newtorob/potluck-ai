"""Whole-claim snippet budgeting over the unchanged v5 ranked prefix."""
from copy import deepcopy

import aiosqlite

from . import investigation_reviewed_collection_v5 as v5
from . import investigation_reviewed_evidence as reviewed
from .memory_db import get_memory_db_path

VERSION = 'reviewed-collection-v6'


def _pack(base):
    result = deepcopy(base['result'])
    candidates = result['reviewed_claims']
    selected = []
    counts = []
    for claim in candidates:
        complete = deepcopy([*selected, claim])
        for item in complete:
            item['presented_content'] = item['content']
            for source in item['sources']:
                source['presented_content'] = source['content']
        snippets, trace = reviewed.snippets(complete)
        counts.append({'claim_id': claim['id'], 'snippet_count': len(snippets),
                       'omitted_snippets': trace['omitted_snippets']})
        if trace['truncated']:
            if not selected:
                raise ValueError('Highest ranked reviewed evidence exceeds snippet limit')
            break
        selected.append(claim)
    omitted = candidates[len(selected):]
    old_trace = result['retrieval_trace']
    packing = {'version': VERSION, 'snippet_limit': trace['max_snippets'],
               'attempts': counts, 'selected_claim_ids': [x['id'] for x in selected],
               'omitted_claim_ids': [x['id'] for x in omitted],
               'stop_reason': 'snippet_limit' if omitted else None}
    old_trace['v5_version'] = base['version']
    old_trace['version'] = VERSION
    old_trace['v5_selected_claim_ids'] = old_trace['selected_claim_ids']
    old_trace['v5_stop_reason'] = old_trace['stop_reason']
    old_trace['v5_claim_characters'] = old_trace['claim_characters']
    old_trace['v5_presentation_characters'] = old_trace['presentation_characters']
    old_trace['claim_characters'] = sum(len(x['content']) for x in selected)
    old_trace['presentation_characters'] = sum(
        len(x['content']) + sum(len(src['content']) for src in x['sources']) for x in selected)
    if omitted:
        old_trace['stop_reason'] = 'snippet_limit'
    old_trace['selected_claim_ids'] = packing['selected_claim_ids']
    old_trace['omitted_claim_ids'] = packing['omitted_claim_ids'] + old_trace['omitted_claim_ids']
    old_trace['omitted_count'] = len(old_trace['omitted_claim_ids'])
    old_trace['snippet_packing'] = packing
    result['reviewed_claims'] = selected
    result['retrieval_version'] = VERSION
    result['truncated'] = bool(old_trace['omitted_count'])
    result['limitations'].append('Snippet budgeting omits whole lower-ranked claims; it does not establish complete coverage.')
    body = {'version': VERSION, 'v5_receipt': base, 'result': result}
    return {'version': VERSION, 'snapshot_sha256': reviewed._digest(body), 'result': result}


async def prepare_in_transaction(db, scope, query):
    return _pack(await v5.prepare_in_transaction(db, scope, query))


async def prepare(scope, query):
    from .investigations import _privacy
    _privacy()
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await db.execute('BEGIN')
        return await prepare_in_transaction(db, scope, query)
