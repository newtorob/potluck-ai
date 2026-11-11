import asyncio
import json

import pytest

from potluck_local.services import investigation_reviewed_collection as gate
from potluck_local.services import investigation_reviewed_evidence as previous
from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
from potluck_local.services.reviewed_knowledge import approve_claim
from potluck_local.services.source_collections import scan_collection
from test_investigation_reviewed_evidence import setup_case, claim  # noqa: F401


def test_support_document_discovery_without_claim_overlap(setup_case):
    async def run():
        scope, folder, _ = await setup_case()
        (folder / 'old.md').write_text('# Overview\nSearchterm feature is here.\n\n# Approved rule\nOld policy applies.\n')
        await scan_collection('w', scope.collection_ids[0])
        result = await lookup_evidence(scope, 'policy', 10, retrieval_version='v2')
        source = next(x for x in result['source_chunks'] if x['relative_path'] == 'old.md' and 'policy' in x['content'])
        ref = {k: source[k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}
        cid = await claim(scope, ref, 'rule', 'Authorized rule applies')
        with pytest.raises(ValueError): await previous.prepare(scope, 'searchterm')
        receipt = await gate.prepare(scope, 'searchterm')
        assert [x['id'] for x in receipt['result']['reviewed_claims']] == [cid]
        assert receipt['result']['source_chunks'] == []
        assert receipt['result']['retrieval_trace']['discovered'][0]['document_id'] == source['document_id']
    asyncio.run(run())


def test_direct_and_conflicting_claims_retained_no_raw_fallback(setup_case):
    async def run():
        scope, _, src = await setup_case()
        with pytest.raises(ValueError, match='No matching'): await gate.prepare(scope, 'rollout')
        ids = {await claim(scope, src['old.md'], 'first', 'Rollout allowed'), await claim(scope, src['new.md'], 'second', 'Rollout forbidden')}
        result = (await gate.prepare(scope, 'rollout'))['result']
        assert {x['id'] for x in result['reviewed_claims']} == ids and result['source_chunks'] == []
        with pytest.raises(ValueError): await gate.prepare(EvidenceScope('other', scope.collection_ids), 'rollout')
    asyncio.run(run())


def test_discovery_and_claim_bounds_fail_closed(setup_case, monkeypatch):
    async def run():
        scope, _, src = await setup_case()
        await claim(scope, src['old.md'], 'first', 'Rollout allowed')
        await claim(scope, src['new.md'], 'second', 'Rollout forbidden')
        monkeypatch.setattr(gate, 'MAX_DISCOVERY_CANDIDATES', 1)
        with pytest.raises(ValueError, match='discovery limit'): await gate.prepare(scope, 'rollout')
        monkeypatch.setattr(gate, 'MAX_DISCOVERY_CANDIDATES', 200); monkeypatch.setattr(gate, 'MAX_CLAIMS', 1)
        with pytest.raises(ValueError, match='claim limit'): await gate.prepare(scope, 'rollout')
    asyncio.run(run())


def test_new_approval_during_generation_rejects(setup_case, monkeypatch):
    async def run():
        scope, _, src = await setup_case()
        await claim(scope, src['old.md'], 'one', 'Rollout yes')
        other = await claim(scope, src['new.md'], 'two', 'Rollout no', False)
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def chat(self, messages, **kwargs):
                payload = json.loads(messages[1]['content'])
                await approve_claim('w', scope.collection_ids, other, 'owner', 'reviewed')
                return {'choices': [{'message': {'content': json.dumps({'R1': {'groups': [{'focus': 'Rollout', 'snippet_ids': [payload['snippets'][0]['snippet_id']], 'status': 'supported'}], 'more': False}})}}]}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        with pytest.raises(ValueError, match='Reviewed evidence changed'):
            await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21', reviewed_operations=[{'request':'Rollout policy?', 'operation':'answer'}], evidence_selection=gate.VERSION)
    asyncio.run(run())


def test_direct_claim_match_without_matching_document(setup_case):
    async def run():
        scope, _, src = await setup_case()
        cid = await claim(scope, src['old.md'], 'special', 'Uniqueclaimtoken approved')
        receipt = await gate.prepare(scope, 'uniqueclaimtoken')
        assert receipt['result']['retrieval_trace']['discovered'] == []
        assert [x['id'] for x in receipt['result']['reviewed_claims']] == [cid]
    asyncio.run(run())


def test_source_change_during_generation_fails_before_save(setup_case, monkeypatch):
    async def run():
        scope, folder, src = await setup_case()
        await claim(scope, src['old.md'], 'one', 'Rollout yes')
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def chat(self, messages, **kwargs):
                payload = json.loads(messages[1]['content'])
                (folder / 'old.md').write_text('# Changed\nRollout changed.\n')
                await scan_collection('w', scope.collection_ids[0])
                return {'choices': [{'message': {'content': json.dumps({'R1': {'groups': [{'focus': 'Rollout', 'snippet_ids': [payload['snippets'][0]['snippet_id']], 'status': 'supported'}], 'more': False}})}}]}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        with pytest.raises(ValueError):
            await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21', reviewed_operations=[{'request':'Rollout policy?', 'operation':'answer'}], evidence_selection=gate.VERSION)
    asyncio.run(run())


def test_stale_scope_and_presentation_limit_reject(setup_case, monkeypatch):
    import aiosqlite
    from potluck_local.services.memory_db import get_memory_db_path
    async def run():
        scope, _, src = await setup_case()
        await claim(scope, src['old.md'], 'one', 'Rollout yes')
        monkeypatch.setattr(gate, 'MAX_TOTAL_CHARS', 1)
        with pytest.raises(ValueError, match='presentation limit'): await gate.prepare(scope, 'rollout')
        monkeypatch.setattr(gate, 'MAX_TOTAL_CHARS', 24000)
        async with aiosqlite.connect(get_memory_db_path()) as db:
            await db.execute("UPDATE source_collections SET scan_status='failed' WHERE id=?", (scope.collection_ids[0],)); await db.commit()
        with pytest.raises(ValueError, match='ready and recent'): await gate.prepare(scope, 'rollout')
    asyncio.run(run())
