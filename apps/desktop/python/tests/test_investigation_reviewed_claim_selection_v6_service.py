import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection_v6 as selection


def raw(ids):
    return json.dumps({'unit_ids': ids, 'verdict': 'supported'})


def test_real_service_source_render_get_review_export_and_revocation(tmp_path, monkeypatch):
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

    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def validate_investigation_profile(self, *args, **kwargs): return None
        async def measure_investigation_prompt(self, messages, **kwargs):
            return {'input_tokens': 123, 'context_tokens': 8192}
        async def chat(self, messages, **kwargs):
            assert list(kwargs['response_format']['schema']['properties']) == ['unit_ids', 'verdict']
            return {'choices': [{'message': {'content': raw(['U2'])}}]}

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
        item = await inv.create_investigation(scope, 'Which rollout build?', 'rollout', 'model',
            template_version='investigation-v21', reviewed_operations=[{'request': 'Which rollout build?', 'operation': 'answer'}],
            evidence_selection='reviewed-current-v1', presentation_mode=selection.VERSION,
            generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1',
            reliability_mode='bounded-evidence-v1')
        text = item['brief']['answers'][0]['text']
        assert all(value in text for value in ('Only approved', 'signed build', 'rollback record', 'incident remains open'))
        assert item['brief']['answers'][0]['passage_presentation']['version'] == selection.VERSION
        assert 'rendered from source' in item['provenance']['factual_generation']
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
        corrected['answers'][0]['text'] = 'Use a signed build after approval; wait while the incident remains open.'
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
