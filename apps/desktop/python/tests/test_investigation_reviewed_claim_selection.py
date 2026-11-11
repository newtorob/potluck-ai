import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection as selection


def prepared():
    evidence = [{'kind':'reviewed_claim','citation_id':f'claim:{i}','content':text,
                 'sources':[{'citation_id':f'source:{i}:revision','content':f'support {i}'}]}
                for i, text in enumerate(['First rule.\n\nIt has a condition.', 'A conflicting rule.'], 1)]
    return selection.prepare('What is the rule?', evidence)


def test_full_claims_and_support_survive_exactly():
    p = prepared(); result = selection.decode('{"status":"conflicting","claim_ids":["C1","C2"]}', p)
    assert result['text'] == '[C1] First rule.\n\nIt has a condition.\n[C2] A conflicting rule.'
    assert result['claims'][0]['sources'] == p['claims'][0]['sources']
    assert result['needs_review'] and 'selected reviewed statements' in result['scope']


@pytest.mark.parametrize('raw', ['{"status":"supported","claim_ids":[]}', '{"status":"conflicting","claim_ids":["C1"]}',
    '{"status":"unknown","claim_ids":["C1","C1"]}', '{"status":"unknown","claim_ids":["C9"]}',
    '{"status":"unknown","claim_ids":[],"extra":true}'])
def test_invalid_selection_fails(raw):
    with pytest.raises(ValueError): selection.decode(raw, prepared())


def test_binding_and_budget_fail_closed(monkeypatch):
    p = prepared(); bad = deepcopy(p); bad['claims'][0]['text'] = 'changed'
    with pytest.raises(ValueError): selection.messages(bad)
    monkeypatch.setattr(selection, 'MAX_VISIBLE_CHARS', 10)
    with pytest.raises(ValueError, match='budget'): selection.decode('{"status":"supported","claim_ids":["C1"]}', p)


def test_original_replay_rebuilds_from_evidence_and_detects_tampering():
    catalog = prepared()
    evidence = [{'kind': 'reviewed_claim', 'citation_id': row['citation_id'],
                 'content': row['text'], 'sources': deepcopy(row['sources'])}
                for row in catalog['claims']]
    raw = '{"status":"supported","claim_ids":["C1"]}'
    original = selection.decode(raw, catalog)
    assert selection.validate_original('What is the rule?', evidence, raw, original) == original
    evidence[0]['sources'][0]['content'] = 'changed support'
    with pytest.raises(ValueError, match='original'):
        selection.validate_original('What is the rule?', evidence, raw, original)
    evidence[0]['sources'][0]['content'] = 'support 1'
    original['text'] = 'altered original'
    with pytest.raises(ValueError, match='original'):
        selection.validate_original('What is the rule?', evidence, raw, original)


def test_real_save_get_correct_and_original_replay(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.services import investigation_verification as verification
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def validate_investigation_profile(self, *args, **kwargs): return None
        async def measure_investigation_prompt(self, messages, **kwargs):
            Runtime.measured = (deepcopy(messages), deepcopy(kwargs['response_format']['schema']))
            return {'input_tokens': 123, 'context_tokens': 8192}
        async def chat(self, messages, **kwargs):
            Runtime.actual = (deepcopy(messages), deepcopy(kwargs['response_format']['schema']))
            assert 'claims' in json.loads(messages[1]['content'])
            return {'choices': [{'message': {'content': '{"status":"supported","claim_ids":["C1"]}'}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'; folder.mkdir()
        (folder / 'source.md').write_text('# Rule\nRollout policy includes an earlier rule.\n')
        cid = await register_collection('w', 'docs', folder); await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        result = await lookup_evidence(scope, 'rollout', 10, retrieval_version='v2')
        source = {k: result['source_chunks'][0][k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}
        claim_id = await propose_claim('w', (cid,), 'rollout', 'Rollout current rule.\n\nA second exact paragraph.', [source])
        await approve_claim('w', (cid,), claim_id, 'owner', 'reviewed')
        item = await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21',
            reviewed_operations=[{'request': 'Rollout policy?', 'operation': 'answer'}],
            evidence_selection='reviewed-current-v1', presentation_mode=selection.VERSION)
        answer = item['brief']['answers'][0]
        assert 'Rollout current rule.\n\nA second exact paragraph.' in answer['text']
        assert 'Supporting source (not independently approved)' in answer['text']
        assert answer['snippet_ids'] and all('[' + sid + ']' in answer['text'] for sid in answer['snippet_ids'])
        retrieved = await inv.get_investigation(scope, item['id'])
        assert retrieved['brief'] == item['brief']
        with pytest.raises(ValueError, match='Automatic verification is unsupported'):
            verification.original_input(item)
        forged = deepcopy(item)
        forged['evidence'][0]['content'] = 'Changed claim.'
        with pytest.raises(ValueError, match='trace|original'):
            verification.original_input(forged)
        corrected = deepcopy(item['brief'])
        corrected['answers'][0]['text'] = 'Owner correction based on the cited sources.'
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'review', corrected_brief=corrected)
        assert reviewed['review']['corrected_brief']['answers'][0]['text'] == 'Owner correction based on the cited sources.'
        assert reviewed['brief'] == item['brief']
        measured = await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21',
            reviewed_operations=[{'request': 'Rollout policy?', 'operation': 'answer'}],
            evidence_selection='reviewed-current-v1', presentation_mode=selection.VERSION,
            generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1', reliability_mode='bounded-evidence-v1')
        budget = measured['provenance']['input_budget']
        assert budget['model_visible_claim_ids'] == ['C1']
        assert 'not in the model prompt' in budget['support_visibility']
        assert budget['input_tokens'] == 123 and budget['context_tokens'] == 8192
        assert 'selected_snippet_ids' not in budget and 'omitted_snippet_ids' not in budget
        assert Runtime.measured == Runtime.actual
        import hashlib
        digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        assert budget['messages_sha256'] == digest(Runtime.actual[0])
        assert budget['schema_sha256'] == digest(Runtime.actual[1])
        assert budget['model_visible_claim_ids'] == [c['id'] for c in json.loads(Runtime.actual[0][1]['content'])['claims']]
        corrupted = deepcopy(measured)
        corrupted['provenance']['input_budget']['messages_sha256'] = '0' * 64
        with pytest.raises(ValueError, match='budget trace'):
            verification.original_input(corrupted)
    asyncio.run(run())


def test_malformed_inputs_and_oversized_response_fail():
    p = prepared()
    with pytest.raises(ValueError):
        selection.decode(' ' * (selection.MAX_RAW_CHARS + 1), p)
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:1', 'content': 'Rule',
                 'sources': [{'citation_id': 'source:1:r', 'content': 'Source'},
                             {'citation_id': 'source:1:r', 'content': 'Source'}]}]
    with pytest.raises(ValueError, match='Duplicate'):
        selection.prepare('Rule?', evidence)
    evidence[0]['sources'] = [{'citation_id': 'foreign', 'content': 'Source'}]
    with pytest.raises(ValueError, match='support'):
        selection.prepare('Rule?', evidence)


def test_no_selection_is_scoped_and_support_counts_in_budget(monkeypatch):
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:1', 'content': 'Claim',
                 'sources': [{'citation_id': 'source:1:r', 'content': 'Source'}]}]
    from potluck_local.services import investigation_reviewed_evidence
    snippets, _ = investigation_reviewed_evidence.snippets(evidence)
    with pytest.raises(ValueError, match='catalog'):
        selection.make_brief('Rule?', {'id': 'R1', 'request': 'Rule?', 'operation': 'answer'}, evidence, snippets[:1], '{\"status\":\"unknown\",\"claim_ids\":[]}')
    op = {'id': 'R1', 'request': 'Rule?', 'operation': 'answer'}
    brief, _ = selection.make_brief('Rule?', op, evidence, snippets, '{"status":"unknown","claim_ids":[]}')
    assert 'No reviewed statement was selected' in brief['answers'][0]['text']
    monkeypatch.setattr(selection, 'MAX_VISIBLE_CHARS', 150)
    with pytest.raises(ValueError, match='budget'):
        selection.make_brief('Rule?', op, evidence, snippets, '{"status":"supported","claim_ids":["C1"]}')


def test_lifecycle_change_during_generation_rejects_save(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import aiosqlite
    from potluck_local.services import investigations as inv
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db, get_memory_db_path
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim
    from potluck_local.services.source_collections import register_collection, scan_collection
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    async def run():
        await init_memory_db(); folder = tmp_path / 'docs'; folder.mkdir()
        (folder / 'source.md').write_text('Rollout policy is current.\n')
        cid = await register_collection('w', 'docs', folder); await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        found = await lookup_evidence(scope, 'rollout', 10, retrieval_version='v2')
        source = {k: found['source_chunks'][0][k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}
        claim_id = await propose_claim('w', (cid,), 'rollout', 'Rollout approved.', [source])
        await approve_claim('w', (cid,), claim_id, 'owner', 'reviewed')
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def chat(self, messages, **kwargs):
                async with aiosqlite.connect(get_memory_db_path()) as db:
                    await db.execute("UPDATE reviewed_claims SET status='deleted' WHERE id=?", (claim_id,)); await db.commit()
                return {'choices': [{'message': {'content': '{"status":"supported","claim_ids":["C1"]}'}}]}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        with pytest.raises(ValueError):
            await inv.create_investigation(scope, 'Rollout?', 'rollout', 'model', template_version='investigation-v21',
                reviewed_operations=[{'request':'Rollout?', 'operation':'answer'}],
                evidence_selection='reviewed-current-v1', presentation_mode=selection.VERSION)
        async with aiosqlite.connect(get_memory_db_path()) as db:
            assert (await (await db.execute('SELECT COUNT(*) FROM investigations')).fetchone())[0] == 0
    asyncio.run(run())


def test_schema_admitted_and_unknown_explicit_statement_differs_from_no_selection():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    p = prepared()
    fmt = {'type': 'json_object', 'schema': selection.response_schema(p)}
    assert _structured_response_format(object.__new__(SplitModel), fmt) == fmt
    absent = selection.decode('{"status":"unknown","claim_ids":[]}', p)
    selected = selection.decode('{"status":"unknown","claim_ids":["C1"]}', p)
    assert absent['claims'] == [] and selected['claims'][0]['text'] == p['claims'][0]['text']


def test_input_claim_cap_and_passage_cap_are_explicit(monkeypatch):
    from potluck_local.services import investigation_reviewed_evidence
    claims = [{'kind': 'reviewed_claim', 'citation_id': f'claim:{i}', 'content': f'Claim {i}',
               'sources': [{'citation_id': f'source:{i}:r', 'content': f'Source {i}'}]}
              for i in range(17)]
    with pytest.raises(ValueError, match='input'):
        selection.prepare('Question?', claims)
    claims = claims[:2]
    snippets, _ = investigation_reviewed_evidence.snippets(claims)
    monkeypatch.setattr(selection, 'MAX_CLAIMS', 3)
    with pytest.raises(ValueError, match='budget'):
        selection.make_brief('Question?', {'id': 'R1', 'request': 'Question?', 'operation': 'answer'},
                             claims, snippets, '{"status":"conflicting","claim_ids":["C1","C2"]}')
