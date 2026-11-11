"""Ignore grammatical query noise without changing reviewed authority boundaries."""
from copy import deepcopy

import aiosqlite

from . import investigation_reviewed_collection as previous
from .evidence_lookup import EvidenceScope, _terms
from .investigation_reviewed_evidence import _digest
from .memory_db import get_memory_db_path

VERSION = 'reviewed-collection-v2'
# These words describe a question's grammar, not a source's subject matter.
FUNCTION_WORDS = frozenset({
    's', 't', 're', 've', 'll', 'd', 'm',
    'i', 'me', 'my', 'mine', 'we', 'us', 'our', 'ours',
    'you', 'your', 'yours', 'he', 'him', 'his', 'she', 'her', 'hers',
    'they', 'them', 'their', 'theirs', 'its', 'itself', 'themselves',
    'this', 'that', 'these', 'those', 'there',
    'should', 'would', 'could', 'can', 'will', 'shall',
    'has', 'have', 'had', 'been', 'being',
})


async def prepare_in_transaction(db, scope, query):
    if not isinstance(scope, EvidenceScope) or not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError('Invalid reviewed collection request')
    original = _terms(query)
    retained = original - FUNCTION_WORDS
    if not retained:
        raise ValueError('No matching reviewed evidence')
    effective = ' '.join(sorted(retained))
    receipt = await previous.prepare_in_transaction(db, scope, effective)
    result = deepcopy(receipt['result'])
    result['retrieval_version'] = VERSION
    result['retrieval_trace']['query_terms'] = {
        'version': 'grammatical-filter-v1', 'retained': sorted(retained),
        'ignored_function_words': sorted(original - retained),
    }
    result['limitations'].append('Grammatical query terms are ignored; semantic relevance is not verified.')
    return {'version': VERSION, 'snapshot_sha256': _digest({
        'version': VERSION, 'query': query, 'effective_query': effective,
        'selection_snapshot_sha256': receipt['snapshot_sha256'],
    }), 'result': result}


async def prepare(scope, query):
    from .investigations import _privacy
    _privacy()
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await db.execute('BEGIN')
        return await prepare_in_transaction(db, scope, query)
