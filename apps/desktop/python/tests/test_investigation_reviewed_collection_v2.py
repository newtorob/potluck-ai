import asyncio

import pytest

from potluck_local.services import investigation_reviewed_collection as v1
from potluck_local.services import investigation_reviewed_collection_v2 as v2
from test_investigation_reviewed_evidence import setup_case, claim  # noqa: F401


def test_function_word_matches_do_not_select_unrelated_claims(setup_case):
    async def run():
        scope, _, src = await setup_case()
        target = await claim(scope, src['old.md'], 'launch', 'Launch requires approval')
        other = await claim(scope, src['new.md'], 'receipt', "Their colleague's receipt is pending")
        query = "How should their colleague's launch work?"
        assert {x['id'] for x in (await v1.prepare(scope, query))['result']['reviewed_claims']} == {target, other}
        # A genuine domain term still selects the other claim; grammar filtering is not a semantic ranker.
        query = "How should their launch work?"
        receipt = await v2.prepare(scope, query)
        assert [x['id'] for x in receipt['result']['reviewed_claims']] == [target]
        assert receipt['result']['retrieval_trace']['query_terms']['ignored_function_words'] == ['should', 'their']
        assert receipt == await v2.prepare(scope, query)
    asyncio.run(run())


def test_conflicts_and_direct_matches_remain_complete(setup_case):
    async def run():
        scope, _, src = await setup_case()
        ids = {await claim(scope, src['old.md'], 'one', 'Rollout allowed'), await claim(scope, src['new.md'], 'two', 'Rollout forbidden')}
        receipt = await v2.prepare(scope, 'Should we allow rollout?')
        assert {c['id'] for c in receipt['result']['reviewed_claims']} == ids
        assert receipt['result']['source_chunks'] == []
        assert receipt['version'] == v2.VERSION
    asyncio.run(run())


def test_filtered_empty_query_and_no_authority_fail_closed(setup_case):
    async def run():
        scope, _, _ = await setup_case()
        for query in ['Should we?', 'their rollout', "it's"]:
            with pytest.raises(ValueError, match='No matching reviewed evidence'):
                await v2.prepare(scope, query)
    asyncio.run(run())


def test_new_approval_changes_receipt_even_when_unmatched(setup_case):
    async def run():
        scope, _, src = await setup_case()
        await claim(scope, src['old.md'], 'one', 'Rollout allowed')
        before = await v2.prepare(scope, 'their rollout')
        await claim(scope, src['new.md'], 'other', 'Unrelated information')
        after = await v2.prepare(scope, 'their rollout')
        assert before['snapshot_sha256'] != after['snapshot_sha256']
    asyncio.run(run())
