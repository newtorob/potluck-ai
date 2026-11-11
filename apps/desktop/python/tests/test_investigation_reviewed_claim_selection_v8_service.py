import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection_v8 as selection


def raw(ids):
    return json.dumps({'unit_ids': ids})


@pytest.mark.parametrize('known', [True, False])
def test_real_service_source_render_get_review_export_and_revocation(tmp_path, monkeypatch, known):
    import asyncio
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.routers import investigations as routes
    from potluck_local.services import investigation_verification as verification
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))

    question = 'Which rollout build?' if known else 'Which rollout build and which exact time?'
    calls = []; measurements = []
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def validate_investigation_profile(self, *args, **kwargs): return None
        async def measure_investigation_prompt(self, messages, **kwargs):
            measurements.append(deepcopy((messages, kwargs)))
            return {'input_tokens': 123 + len(measurements), 'context_tokens': 8192}
        async def chat(self, messages, **kwargs):
            calls.append(deepcopy((messages, kwargs)))
            expected = ['unit_ids'] if len(calls) == 1 else ['review_unit_ids', 'verdict']
            assert list(kwargs['response_format']['schema']['properties']) == expected
            reply = raw(['U2']) if len(calls) == 1 else json.dumps({'review_unit_ids': ['U2'], 'verdict': {'supported': None} if known else {'unknown': 'exact time'}})
            return {'choices': [{'message': {'content': reply}}]}

    monkeypatch.setattr(inv, 'get_runtime', Runtime)

    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'; folder.mkdir()
        body = 'Only approved rollout releases may proceed:\n- Use a signed build.\n- Keep the rollback record.\n\nUnless the incident remains open, in which case wait.'
        source = folder / 'source.md'; source.write_text('# Rollout\n\n' + body)
        collection = await register_collection('w', 'docs', folder)
        await scan_collection('w', collection)
        scope = EvidenceScope('w', (collection,))
        found = await lookup_evidence(scope, 'rollout', 10, retrieval_version='v2')
        refs = [{k: found['source_chunks'][0][k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}]
        claim = await propose_claim('w', (collection,), 'rollout', body, refs)
        await approve_claim('w', (collection,), claim, 'test-reviewer', 'fixture source checked')
        item = await inv.create_investigation(scope, question, 'rollout', 'model',
            template_version='investigation-v21', reviewed_operations=[{'request': question, 'operation': 'answer'}],
            evidence_selection='reviewed-current-v1', presentation_mode=selection.VERSION,
            generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1',
            reliability_mode='bounded-evidence-v1')
        text = item['brief']['answers'][0]['text']
        assert all(value in text for value in ('Only approved', 'signed build', 'rollback record', 'incident remains open'))
        assert item['brief']['answers'][0]['status'] == ('supported' if known else 'unknown')
        if not known:
            assert text.startswith('The selected reviewed statements do not specify: exact time')
        assert item['brief']['answers'][0]['passage_presentation']['version'] == 'reviewed-claim-selection-v6'
        assert 'rendered from source' in item['provenance']['factual_generation']
        assert [c[1]['max_tokens'] for c in calls] == [2000, 512]
        assert [c[1]['expected_context_tokens'] for c in calls] == [8192, 8192]
        assert [c[1]['expected_sha256'] for c in calls] == ['a'*64, 'a'*64]
        assert len(measurements) == 2
        assert measurements[0][0] == calls[0][0] and measurements[1][0] == calls[1][0]
        trace = item['provenance']['reviewed_claim_selection']
        review_input = item['provenance']['reviewed_claim_review_input']
        assert trace['version'] == selection.VERSION
        assert trace['renderer_version'] == 'reviewed-claim-selection-v6'
        assert review_input['input_tokens'] == 125 and review_input['output_tokens'] == 512
        assert review_input['retained_unit_ids'] == trace['selection']['unit_ids']
        for field, value in [('input_tokens', True), ('input_tokens', 8192), ('context_tokens', 4096), ('output_tokens', 2000), ('retained_unit_ids', [])]:
            broken = deepcopy(item); broken['provenance']['reviewed_claim_review_input'][field] = value
            with pytest.raises(ValueError):
                selection.validate_brief(broken, item['provenance']['snippet_catalog'])
        assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
        exported = (await routes.export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        assert 'Presented reviewed claim IDs: C1' in exported and 'incident remains open' in exported
        with pytest.raises(ValueError, match='Automatic verification is unsupported'):
            verification.original_input(item)
        forged = deepcopy(item['brief']); forged['answers'][0]['source_excerpts'][0]['quote'] += ' forged'
        with pytest.raises(inv.BriefValidationError):
            await inv.review_investigation(scope, item['id'], 'correct', 'test-reviewer', 'tamper test', corrected_brief=forged)
        assert (await inv.get_investigation(scope, item['id']))['review'] is None
        corrected = deepcopy(item['brief'])
        corrected['answers'][0]['text'] = 'Use a signed build after approval; wait while the incident remains open.' + ('' if known else ' Exact time is unstated in the selected statements.')
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'test-reviewer', 'fixture correction', corrected_brief=corrected)
        assert reviewed['brief'] == item['brief']
        reread = await inv.get_investigation(scope, item['id'])
        assert reread['review']['corrected_brief']['answers'][0]['text'] == corrected['answers'][0]['text']
        assert 'passage_presentation' not in reread['review']['corrected_brief']['answers'][0]
        exported = (await routes.export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        assert 'The sections above use the reviewed correction. The original draft is retained in Potluck.' in exported
        source.write_text('# Rollout\n\nA changed source.'); await scan_collection('w', collection)
        with pytest.raises((ValueError, LookupError)):
            await inv.get_investigation(scope, item['id'])
    asyncio.run(run())


@pytest.mark.parametrize('failure,expected_calls,expected_measurements', [
    ('invalid_first', 1, 1), ('second_overflow', 1, 2), ('changed_capacity', 1, 2),
    ('invalid_review', 2, 2), ('source_change', 2, 2), ('changed_model', 2, 2),
    ('deadline_before_review', 1, 1), ('deadline_during_review', 2, 2),
])
def test_failed_pipeline_never_retries_or_saves(tmp_path, monkeypatch, failure, expected_calls, expected_measurements):
    import asyncio
    import aiosqlite
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim
    from potluck_local.services.source_collections import register_collection, scan_collection
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    calls = []; measurements = []; altered = False
    # Override only this module's clock; changing the global time module breaks asyncio.
    clock = [100.0]
    monkeypatch.setattr(inv, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    async def run():
        nonlocal altered
        await init_memory_db()
        folder = tmp_path / 'docs'; folder.mkdir()
        body = 'A signed build requires approval.'
        source = folder / 'source.md'; source.write_text('# Rollout\n\n' + body)
        collection = await register_collection('w', 'docs', folder); await scan_collection('w', collection)
        scope = EvidenceScope('w', (collection,))
        found = await lookup_evidence(scope, 'rollout', 10, retrieval_version='v2')
        refs = [{k: found['source_chunks'][0][k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}]
        claim = await propose_claim('w', (collection,), 'rollout', body, refs)
        await approve_claim('w', (collection,), claim, 'fixture', 'source checked')
        class Runtime:
            def current_model_id(self): return 'changed' if altered else 'model'
            def current_model_sha256(self): return 'a' * 64
            async def validate_investigation_profile(self, *args, **kwargs): return None
            async def measure_investigation_prompt(self, messages, **kwargs):
                measurements.append(messages)
                if len(measurements) == 2:
                    if failure == 'second_overflow': return {'input_tokens': 7681, 'context_tokens': 8192}
                    if failure == 'changed_capacity': return {'input_tokens': 100, 'context_tokens': 4096}
                return {'input_tokens': 100, 'context_tokens': 8192}
            async def chat(self, messages, **kwargs):
                nonlocal altered
                calls.append(messages)
                if len(calls) == 1:
                    if failure == 'deadline_before_review': clock[0] += 241
                    reply = '{"unit_ids":["missing"]}' if failure == 'invalid_first' else raw(['U1'])
                else:
                    if failure == 'source_change':
                        source.write_text('# Rollout\n\nChanged policy.'); await scan_collection('w', collection)
                    if failure == 'changed_model': altered = True
                    if failure == 'deadline_during_review':
                        # Exactly the remaining shared deadline is allocated, never a reset.
                        assert asyncio.current_task().cancelling() == 0
                        raise asyncio.TimeoutError
                    reply = json.dumps({'review_unit_ids': ['U1'], 'verdict': {'invalid' if failure == 'invalid_review' else 'supported': None}})
                return {'choices': [{'message': {'content': reply}}]}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        with pytest.raises(ValueError):
            await inv.create_investigation(scope, 'Which rollout build?', 'rollout', 'model',
                template_version='investigation-v21', reviewed_operations=[{'request':'Which rollout build?', 'operation':'answer'}],
                evidence_selection='reviewed-current-v1', presentation_mode=selection.VERSION,
                generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1',
                reliability_mode='bounded-evidence-v1')
        assert len(calls) == expected_calls and len(measurements) == expected_measurements
        async with aiosqlite.connect(tmp_path / 'db.sqlite') as db:
            row = await (await db.execute('SELECT COUNT(*) FROM investigations')).fetchone()
            assert row[0] == 0
    asyncio.run(run())
