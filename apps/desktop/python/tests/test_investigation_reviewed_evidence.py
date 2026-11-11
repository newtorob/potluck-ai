import asyncio
import json
from types import SimpleNamespace

import aiosqlite
import pytest

from potluck_local.services import investigation_reviewed_evidence as gate
from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
from potluck_local.services.memory_db import init_memory_db, get_memory_db_path
from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim, supersede_claim
from potluck_local.services.source_collections import register_collection, scan_collection


@pytest.fixture
def setup_case(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    async def setup():
        await init_memory_db(); folder = tmp_path / 'docs'; folder.mkdir()
        (folder / 'old.md').write_text('# Old\nRollout old policy applies.\n')
        (folder / 'new.md').write_text('# New\nRollout new policy applies.\n')
        cid = await register_collection('w', 'docs', folder); await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,)); result = await lookup_evidence(scope, 'rollout', 10, retrieval_version='v2')
        sources = {x['relative_path']: {k:x[k] for k in ('collection_id','document_id','revision_id','chunk_id')} for x in result['source_chunks']}
        return scope, folder, sources
    return setup


async def claim(scope, source, key, text, approve=True):
    cid = await propose_claim('w', scope.collection_ids, key, text, [source])
    if approve: await approve_claim('w', scope.collection_ids, cid, 'owner', 'reviewed')
    return cid


def test_supersession_and_unreviewed_source_do_not_fallback(setup_case):
    async def run():
        scope, _, src = await setup_case()
        old = await claim(scope, src['old.md'], 'rollout', 'Rollout old policy')
        new = await claim(scope, src['new.md'], 'rollout', 'Rollout new policy', False)
        first = await gate.prepare(scope, 'rollout'); assert [c['id'] for c in first['result']['reviewed_claims']] == [old]
        await supersede_claim('w', scope.collection_ids, old, new, 'owner', 'replacement')
        second = await gate.prepare(scope, 'rollout')
        assert [c['id'] for c in second['result']['reviewed_claims']] == [new]
        assert second['result']['source_chunks'] == []
        assert [s['relative_path'] for s in second['result']['reviewed_claims'][0]['sources']] == ['new.md']
        assert first['snapshot_sha256'] != second['snapshot_sha256']
    asyncio.run(run())


def test_distinct_approved_keys_both_retained_and_no_raw_fallback(setup_case):
    async def run():
        scope, _, src = await setup_case()
        with pytest.raises(ValueError, match='No matching'): await gate.prepare(scope, 'rollout')
        ids = {await claim(scope, src['old.md'], 'one', 'Rollout yes'), await claim(scope, src['new.md'], 'two', 'Rollout no')}
        r = await gate.prepare(scope, 'rollout')
        assert {x['id'] for x in r['result']['reviewed_claims']} == ids
        assert all(x['reviewer'] == 'owner' and x['reason'] == 'reviewed' for x in r['result']['reviewed_claims'])
        with pytest.raises(ValueError): await gate.prepare(EvidenceScope('other', scope.collection_ids), 'rollout')
    asyncio.run(run())


def test_limits_and_stale_fail_closed(setup_case, monkeypatch):
    async def run():
        scope, _, src = await setup_case()
        await claim(scope, src['old.md'], 'one', 'Rollout yes')
        await claim(scope, src['new.md'], 'two', 'Rollout no')
        monkeypatch.setattr(gate, 'MAX_CLAIMS', 1)
        with pytest.raises(ValueError, match='claim limit'): await gate.prepare(scope, 'rollout')
        monkeypatch.setattr(gate, 'MAX_CLAIMS', 100); monkeypatch.setattr(gate, 'MAX_TOTAL_CHARS', 1)
        with pytest.raises(ValueError, match='presentation limit'): await gate.prepare(scope, 'rollout')
        async with aiosqlite.connect(get_memory_db_path()) as db:
            await db.execute("UPDATE source_collections SET scan_status='failed' WHERE id=?", (scope.collection_ids[0],)); await db.commit()
        with pytest.raises(ValueError, match='ready and recent'): await gate.prepare(scope, 'rollout')
    asyncio.run(run())


def test_lifecycle_change_during_generation_rejects_before_save(setup_case, monkeypatch):
    async def run():
        scope, _, src = await setup_case()
        current = await claim(scope, src['old.md'], 'rollout', 'Rollout old policy')
        new = await claim(scope, src['new.md'], 'rollout', 'Rollout new policy', False)
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def chat(self, messages, **kwargs):
                payload = json.loads(messages[1]['content'])
                await supersede_claim('w', scope.collection_ids, current, new, 'owner', 'replacement')
                return {'choices': [{'message': {'content': json.dumps({'R1': {'groups': [{'focus': 'Rollout', 'snippet_ids': [payload['snippets'][0]['snippet_id']], 'status': 'supported'}], 'more': False}})}}]}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        with pytest.raises(ValueError, match='Reviewed evidence changed'):
            await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21', reviewed_operations=[{'request':'Rollout policy?', 'operation':'answer'}], evidence_selection=gate.VERSION)
        async with aiosqlite.connect(get_memory_db_path()) as db:
            assert (await (await db.execute('SELECT COUNT(*) FROM investigations')).fetchone())[0] == 0
    asyncio.run(run())


def test_v21_success_uses_reviewed_only_and_preserves_metadata(setup_case, monkeypatch):
    async def run():
        scope, _, src = await setup_case()
        current = await claim(scope, src['old.md'], 'rollout', 'Rollout current policy requires approval')
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def chat(self, messages, **kwargs):
                payload = json.loads(messages[1]['content'])
                assert 'Only supplied current claims carry human review status' in messages[0]['content']
                links = payload['reviewed_claim_support'][0]
                by_id = {x['snippet_id']: x['text'] for x in payload['snippets']}
                assert any('Rollout current policy requires approval' in by_id[sid] for sid in links['claim_snippet_ids'])
                assert any('Rollout old policy applies' in by_id[sid] for sid in links['support_snippet_ids'])
                return {'choices': [{'message': {'content': json.dumps({'R1': {'groups': [{'focus': 'Rollout', 'snippet_ids': [payload['snippets'][0]['snippet_id']], 'status': 'supported'}], 'more': False}})}}]}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        item = await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21', reviewed_operations=[{'request':'Rollout policy?', 'operation':'answer'}], evidence_selection=gate.VERSION)
        assert [e['id'] for e in item['evidence']] == [current]
        assert item['evidence'][0]['reviewer'] == 'owner'
        assert item['provenance']['snippet_trace']['version'] == 'reviewed-paragraph-v1'
        corrected = json.loads(json.dumps(item['brief']))
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'review', corrected_brief=corrected)
        assert reviewed['review']['corrected_brief']['answers']
        assert item['provenance']['evidence_selection']['version'] == gate.VERSION
        assert 'entailment is not verified' in item['provenance']['evidence_selection']['limitation']
        with pytest.raises(ValueError, match='Unsupported evidence selection'):
            await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', evidence_selection=gate.VERSION)
    asyncio.run(run())


def test_new_approval_during_generation_is_not_silently_omitted(setup_case, monkeypatch):
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


def test_receipt_tampering_and_revocation_fail_replay(setup_case):
    async def run():
        scope, _, src = await setup_case()
        cid = await claim(scope, src['old.md'], 'one', 'Rollout yes')
        receipt = await gate.prepare(scope, 'rollout')
        await gate.verify(scope, 'rollout', receipt)
        forged = json.loads(json.dumps(receipt)); forged['result']['reviewed_claims'][0]['content'] = 'Rollout forged'
        with pytest.raises(ValueError, match='changed'): await gate.verify(scope, 'rollout', forged)
        async with aiosqlite.connect(get_memory_db_path()) as db:
            await db.execute("UPDATE reviewed_claims SET status='deleted' WHERE id=?", (cid,)); await db.commit()
        with pytest.raises(ValueError): await gate.verify(scope, 'rollout', receipt)
    asyncio.run(run())


def test_snippet_cap_rejected_before_any_model_call(setup_case, monkeypatch):
    async def run():
        scope, _, src = await setup_case()
        await claim(scope, src['old.md'], 'one', 'Rollout yes')
        monkeypatch.setattr(inv.investigation_context, 'MAX_SNIPPETS', 0)
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def chat(self, *args, **kwargs): raise AssertionError('must not infer')
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        with pytest.raises(ValueError, match='snippet limit'):
            await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21', reviewed_operations=[{'request':'Rollout policy?', 'operation':'answer'}], evidence_selection=gate.VERSION)
    asyncio.run(run())


def test_support_boundary_preserved_for_structural_reconstruction():
    from potluck_local.services import investigation_structural_context as structural
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:c1', 'content': 'Current rule.',
                 'sources': [{'citation_id': 'source:s:r', 'content': '1. First.\n2. Second.\n',
                              'structural_boundary': {'version': 'section-end-v1', 'section_end': True}}]}]
    flat = gate.flattened(evidence)
    assert 'structural_boundary' not in flat[0]
    assert flat[1]['structural_boundary'] == evidence[0]['sources'][0]['structural_boundary']
    snippets, trace = gate.snippets(evidence)
    structure = structural.catalog(flat, snippets, version='ordered-list-v2')
    assert trace['version'] == 'reviewed-paragraph-v1'
    assert any(block['citation_id'] == 'source:s:r' and block['complete'] for block in structure['blocks'])


def test_budget_omission_rejected_before_chat(setup_case, monkeypatch):
    async def run():
        scope, _, src = await setup_case()
        await claim(scope, src['old.md'], 'one', 'Rollout yes')
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def validate_investigation_profile(self, *args, **kwargs): pass
            async def measure_investigation_prompt(self, *args, **kwargs): return {'input_tokens': 100, 'context_tokens': 8192}
            async def chat(self, *args, **kwargs): raise AssertionError('must not infer')
        async def fake_fit(data, build, measure):
            await measure([], {})
            return [], {}, {}, {'omitted_snippet_ids': ['S1']}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        monkeypatch.setattr(inv.input_budget, 'fit', fake_fit)
        with pytest.raises(ValueError, match='input budget'):
            await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21', reviewed_operations=[{'request':'Rollout policy?', 'operation':'answer'}], generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1', reliability_mode='bounded-evidence-v1', evidence_selection=gate.VERSION)
    asyncio.run(run())


def test_rescan_invalidates_receipt_and_saved_investigation(setup_case, monkeypatch):
    async def run():
        scope, folder, src = await setup_case()
        await claim(scope, src['old.md'], 'one', 'Rollout yes')
        receipt = await gate.prepare(scope, 'rollout')
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def chat(self, messages, **kwargs):
                payload = json.loads(messages[1]['content'])
                return {'choices': [{'message': {'content': json.dumps({'R1': {'groups': [{'focus': 'Rollout', 'snippet_ids': [payload['snippets'][0]['snippet_id']], 'status': 'supported'}], 'more': False}})}}]}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        item = await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21', reviewed_operations=[{'request':'Rollout policy?', 'operation':'answer'}], evidence_selection=gate.VERSION)
        (folder / 'old.md').write_text('# Changed\nRollout changed.\n')
        await scan_collection('w', scope.collection_ids[0])
        with pytest.raises(ValueError): await gate.verify(scope, 'rollout', receipt)
        with pytest.raises(LookupError): await inv.get_investigation(scope, item['id'])
    asyncio.run(run())


def test_review_boundary_instruction_survives_complete_presentation(setup_case, monkeypatch):
    async def run():
        scope, _, src = await setup_case()
        await claim(scope, src['old.md'], 'one', 'Rollout yes')
        captured = []
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def validate_investigation_profile(self, *args, **kwargs): pass
            async def chat(self, messages, **kwargs):
                captured.append(messages[0]['content'])
                raise RuntimeError('synthetic stop')
        async def fake_fit(data, build, measure):
            messages, schema, trace = build(data)
            return messages, schema, trace, {'omitted_snippet_ids': [], 'context_tokens': 8192}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        monkeypatch.setattr(inv.input_budget, 'fit', fake_fit)
        with pytest.raises(inv.InvestigationInferenceError):
            await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21', reviewed_operations=[{'request':'Rollout policy?', 'operation':'answer'}], generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1', reliability_mode='bounded-evidence-v1', unknown_mode='cited-unknown-v4', presentation_mode='cited-components-v1', evidence_selection=gate.VERSION)
        assert len(captured) == 1
        assert 'Only supplied current claims carry human review status' in captured[0]
        assert 'supporting document text is not independently approved' in captured[0]
        assert 'unresolved conflict' in captured[0]
    asyncio.run(run())


def test_reviewed_draft_verification_context_and_offset_binding(setup_case, monkeypatch):
    from copy import deepcopy
    from potluck_local.services import investigation_verification as verification
    async def run():
        scope, _, src = await setup_case()
        await claim(scope, src['old.md'], 'one', 'Rollout current policy')
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def validate_investigation_profile(self, *args, **kwargs): pass
            async def measure_investigation_prompt(self, *args, **kwargs): return {'input_tokens': 1000, 'context_tokens': 8192}
            async def chat(self, messages, **kwargs):
                data = json.loads(messages[1]['content'])
                ids = [s['snippet_id'] for s in data['snippets']]
                result = {'R1': {'groups': [{'focus': 'Rollout policy', 'status': 'supported',
                    'snippet_ids': ids, 'conditions': [], 'exceptions': [], 'distinctions': [],
                    'conclusion': 'Rollout policy applies.', 'unknown_reason': ''}], 'more': False}}
                return {'choices': [{'message': {'content': json.dumps(result)}}]}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        item = await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21', reviewed_operations=[{'request':'Rollout policy?', 'operation':'answer'}], generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1', reliability_mode='bounded-evidence-v1', unknown_mode='cited-unknown-v4', presentation_mode='cited-components-v1', evidence_selection=gate.VERSION)
        envelope = verification.original_input(item, version='draft-verification-v3')
        labels = {s['context']['review_status'] for s in envelope['source_checks']}
        assert labels == {'approved_claim', 'supporting_source'}
        broken = deepcopy(item)
        sid = envelope['source_checks'][0]['snippet_id']
        broken['provenance']['structural_trace']['offsets'][sid]['start'] += 1
        with pytest.raises(ValueError): verification.original_input(broken, version='draft-verification-v3')
    asyncio.run(run())
