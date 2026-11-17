import asyncio
from copy import deepcopy

import pytest

from potluck_local.services import investigation_context, investigation_reviewed_evidence as reviewed
from potluck_local.services import investigation_reviewed_collection_v6 as gate
from potluck_local.services.reviewed_knowledge import delete_claim
from potluck_local.services.source_collections import scan_collection
from test_investigation_reviewed_evidence import setup_case, claim  # noqa: F401


def record(index, count, source=None):
    text = ''.join(f'- Item {i}: retain condition and exception.\n' for i in range(count))
    return {'id': str(index), 'kind': 'reviewed_claim', 'citation_id': f'claim:{index}',
            'content': text, 'sources': [source or {'citation_id': f'source:{index}', 'content': 'Support condition.'}]}


def base(claims):
    return {'version': 'reviewed-collection-v5', 'snapshot_sha256': 'base', 'result': {
        'reviewed_claims': claims, 'retrieval_version': 'reviewed-collection-v5', 'truncated': False,
        'limitations': [], 'retrieval_trace': {'selected_claim_ids': [c['id'] for c in claims],
            'omitted_claim_ids': [], 'omitted_count': 0, 'stop_reason': None,
            'claim_characters': sum(len(c['content']) for c in claims),
            'presentation_characters': sum(len(c['content']) + sum(len(x['content']) for x in c['sources']) for c in claims)}}}


def test_whole_claim_budget_preserves_conditions_and_source_and_base():
    claims = [record(i, 35) for i in range(3)]
    original = base(claims)
    frozen = deepcopy(original)
    packed = gate._pack(original)
    assert original == frozen
    assert packed['result']['reviewed_claims'] == claims[:2]
    trace = packed['result']['retrieval_trace']
    assert trace['version'] == gate.VERSION and trace['v5_version'] == 'reviewed-collection-v5'
    assert trace['selected_claim_ids'] == ['0', '1']
    assert trace['omitted_claim_ids'] == ['2'] and trace['omitted_count'] == 1
    assert trace['v5_selected_claim_ids'] == ['0', '1', '2']
    assert [c['content'].encode() for c in packed['result']['reviewed_claims']] == [c['content'].encode() for c in frozen['result']['reviewed_claims'][:2]]
    assert [[s['content'].encode() for s in c['sources']] for c in packed['result']['reviewed_claims']] == [[s['content'].encode() for s in c['sources']] for c in frozen['result']['reviewed_claims'][:2]]
    assert trace['snippet_packing']['stop_reason'] == 'snippet_limit'
    assert trace['snippet_packing']['attempts'][-1]['omitted_snippets'] > 0
    assert packed['result']['truncated']
    complete = deepcopy(claims[:2])
    for item in complete:
        item['presented_content'] = item['content']
        for source in item['sources']:
            source['presented_content'] = source['content']
    _, snippet_trace = reviewed.snippets(complete)
    assert not snippet_trace['truncated']
    assert all('condition and exception' in c['content'] for c in packed['result']['reviewed_claims'])


def test_shared_source_is_counted_once_and_oversized_first_fails():
    shared = {'citation_id': 'source:shared', 'content': ''.join(f'- Support {i}.\n' for i in range(40))}
    claims = [record(0, 25, shared), record(1, 25, shared)]
    result = gate._pack(base(claims))['result']
    assert result['reviewed_claims'] == claims
    assert result['retrieval_trace']['snippet_packing']['attempts'][-1]['snippet_count'] == 90
    with pytest.raises(ValueError, match='Highest ranked.*snippet limit'):
        gate._pack(base([record(0, 101)]))


def test_omitted_claim_changes_receipt_and_competing_claims_survive_when_fit(setup_case, monkeypatch):
    async def run():
        scope, _, src = await setup_case()
        first = await claim(scope, src['old.md'], 'a', 'Orchard permit required unless rain occurs.')
        second = await claim(scope, src['old.md'], 'b', 'Orchard permit forbidden unless rain occurs.')
        complete = await gate.prepare(scope, 'orchard permit rain')
        assert {c['id'] for c in complete['result']['reviewed_claims']} == {first, second}
        monkeypatch.setattr(investigation_context, 'MAX_SNIPPETS', 2)
        packed = await gate.prepare(scope, 'orchard permit rain')
        omitted = packed['result']['retrieval_trace']['omitted_claim_ids']
        assert len(omitted) == 1
        await delete_claim('w', scope.collection_ids, omitted[0])
        assert packed != await gate.prepare(scope, 'orchard permit rain')
    asyncio.run(run())


def test_omitted_document_revision_changes_receipt(setup_case, monkeypatch):
    async def run():
        scope, folder, src = await setup_case()
        a = await claim(scope, src['old.md'], 'a', 'Orchard permit requires a signed approval.')
        b = await claim(scope, src['new.md'], 'b', 'Orchard permit requires another signature.')
        monkeypatch.setattr(investigation_context, 'MAX_SNIPPETS', 2)
        before = await gate.prepare(scope, 'orchard permit signature')
        omitted = before['result']['retrieval_trace']['omitted_claim_ids']
        assert len(omitted) == 1
        name = 'old.md' if omitted[0] == a else 'new.md'
        (folder / name).write_text('# Changed\n\nThe orchard source has a new revision.')
        await scan_collection('w', scope.collection_ids[0])
        assert before != await gate.prepare(scope, 'orchard permit signature')
    asyncio.run(run())
