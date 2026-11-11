import json

import pytest

from potluck_local.services import investigation_planned_answers as v12

PLAN = [{'id': 'R1', 'question_id': 'Q1', 'request': 'What is ready and what is blocked?', 'operation': 'answer'}]
SNIPPETS = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Pilot is ready.'}]


def test_planner_preserves_gaps_and_rejects_invalid_spans():
    parts, gaps = v12.decode_plan(json.dumps({'R1': ['ready', 'blocked']}), PLAN)
    assert parts['R1'] == [{'id': 'R1P1', 'request_span': 'ready'}, {'id': 'R1P2', 'request_span': 'blocked'}]
    assert ''.join(gaps['R1']) == 'What is  and what is ?'
    for spans in (['ready', 'ready'], ['missing'], ['ready and what', 'what is blocked'], ['blocked', 'ready'], []):
        with pytest.raises(ValueError):
            v12.decode_plan(json.dumps({'R1': spans}), PLAN)
    with pytest.raises(ValueError):
        v12.decode_plan(json.dumps({'R1': ['ready'], 'R2': ['other']}), PLAN)


def test_fixed_ids_unknown_conflict_and_validation():
    parts, _ = v12.decode_plan(json.dumps({'R1': ['ready', 'blocked']}), PLAN)
    supported = {'status': 'supported', 'text': 'Ready.', 'snippet_ids': ['S1']}
    unknown = {'status': 'unknown', 'text': 'Unknown.', 'snippet_ids': []}
    raw = {'R1': {'R1P1': supported, 'R1P2': unknown}}
    brief, _, trace = v12.decode_results(json.dumps(raw), PLAN, SNIPPETS, [], [], parts)
    assert brief['answers'][0]['status'] == 'unknown'
    assert [p['id'] for p in brief['answers'][0]['parts']] == ['R1P1', 'R1P2']
    assert 'blocked' not in ''.join(trace['unmatched_request_text']['R1'])
    for value in ({'R1P1': supported}, {**raw['R1'], 'foreign': unknown}, {'R1P1': {**supported, 'request_span': 'blocked'}, 'R1P2': unknown}):
        with pytest.raises(ValueError):
            v12.decode_results(json.dumps({'R1': value}), PLAN, SNIPPETS, [], [], parts)
    with pytest.raises(ValueError):
        v12.decode_results(json.dumps({'R1': {'R1P1': {**supported, 'text': 'x' * 999}, 'R1P2': unknown}}), PLAN, SNIPPETS, [], [], parts)


def test_real_runtime_grammar():
    llama_cpp = pytest.importorskip('llama_cpp')
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    parts, _ = v12.decode_plan(json.dumps({'R1': ['ready', 'blocked']}), PLAN)
    for schema in (v12.planner_schema(PLAN), v12.response_schema(PLAN, SNIPPETS, [], parts)):
        response = {'type': 'json_object', 'schema': schema}
        assert _structured_response_format(SplitModel.__new__(SplitModel), response) == response
        assert llama_cpp.LlamaGrammar.from_json_schema(json.dumps(schema), verbose=False)


def test_workflow_correction_and_export(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.services.evidence_lookup import EvidenceScope
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    calls = []
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            calls.append(payload)
            assert kwargs['bounded_generation']
            if len(calls) == 1:
                assert set(payload) == {'operations'}
                data = {'R1': ['ready', 'blocked']}
            else:
                assert 'quantities' not in payload and 'extraction_notices' not in payload
                assert payload['issue'] == PLAN[0]['request']
                assert payload['operations'][0]['request'] == PLAN[0]['request']
                assert payload['snippets'][0]['relative_path'] == 'pilot.md'
                assert 'Snippet limit reached; some presented source text was omitted from model input.' in payload['evidence_notices']
                assert payload['answer_parts']['R1'][0]['id'] == 'R1P1'
                assert set(kwargs['response_format']['schema']['properties']['R1']['properties']) == {'R1P1', 'R1P2'}
                data = {'R1': {'R1P1': {'status': 'supported', 'text': 'Pilot ready.', 'snippet_ids': [payload['snippets'][0]['snippet_id']]}, 'R1P2': {'status': 'unknown', 'text': 'Unknown.', 'snippet_ids': []}}}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'
        folder.mkdir()
        (folder / 'pilot.md').write_text('# Pilot\nPilot is ready at 100 ms.\n' + '- pilot ready\n' * 120)
        cid = await register_collection('w', 'docs', folder)
        await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        item = await inv.create_investigation(scope, PLAN[0]['request'], 'pilot', 'model', template_version='investigation-v12', reviewed_operations=[{'request': PLAN[0]['request'], 'operation': 'answer'}], reviewed_plan_origin='fixture_supplied')
        assert item['provenance']['operation_plan_origin'] == 'fixture_supplied'
        assert item['provenance']['answer_part_plan_origin'] == 'planned_unreviewed'
        assert item['provenance']['snippet_trace']['version'] == 'paragraph-v2'
        assert item['provenance']['snippet_trace']['truncated']
        assert item['provenance']['quantity_catalog']
        assert 'extraction_notices' in item['provenance']
        assert item['provenance']['snippet_catalog']
        assert len(calls) == 2
        result = await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'review', corrected_brief=item['brief'])
        assert 'parts' not in result['review']['corrected_brief']['answers'][0]
        assert result['brief']['answers'][0]['parts']
        from potluck_local.routers.investigations import export
        assert await export(item['id'], 'w', [cid])
    asyncio.run(run())


def test_conflicting_part_and_plan_shape():
    parts, _ = v12.decode_plan(json.dumps({'R1': ['ready']}), PLAN)
    conflict = {'status': 'conflicting', 'text': 'Sources disagree.', 'snippet_ids': ['S1']}
    brief, _, _ = v12.decode_results(json.dumps({'R1': {'R1P1': conflict}}), PLAN, SNIPPETS, [], [], parts)
    assert brief['answers'][0]['status'] == 'conflicting'
    with pytest.raises(ValueError):
        v12.decode_plan(json.dumps({'R1': ['ready', 'ready']}), PLAN)
    assert v12.decode_plan('{}', []) == ({}, {})


def test_calculation_only_skips_factual_planner(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.services.evidence_lookup import EvidenceScope
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    calls = []
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            calls.append(payload)
            assert kwargs['bounded_generation'] and kwargs['max_tokens'] == 2000
            assert payload['answer_parts'] == {}
            quantity = payload['quantities']
            assert [q['value'] for q in quantity] == ['100', '200']
            sid = quantity[0]['snippet_id']
            data = {'R1': {'status': 'ready', 'reason': 'Source quantities selected.', 'minuend_id': quantity[1]['id'], 'subtrahend_id': quantity[0]['id'], 'known_input_snippet_ids': [sid], 'competing_snippet_ids': [], 'limitation_snippet_ids': [], 'limitation_basis': 'none'}}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'
        folder.mkdir()
        (folder / 'release.md').write_text('# Release\nLatency changed from 100 to 200 ms.')
        cid = await register_collection('w', 'docs', folder)
        await scan_collection('w', cid)
        item = await inv.create_investigation(EvidenceScope('w', (cid,)), 'How did latency change?', 'release', 'model', template_version='investigation-v12', reviewed_operations=[{'request': 'How did latency change?', 'operation': 'difference'}])
        assert len(calls) == 1
        assert item['provenance']['answer_part_plan'] == {}
        assert item['provenance']['answer_part_plan_origin'] is None
        assert item['provenance']['answer_part_planner_version'] is None
        assert item['brief']['answers'][0]['calculation']['result'] == '0.1'
        assert item['provenance']['calculation_traces'][0]['algorithm_version'] == 'cited-calculations-v3'
    asyncio.run(run())


@pytest.mark.parametrize('trace', [None, {}, {'version': 'unknown'}, [], {'version': None}])
def test_correction_rejects_invalid_stored_trace(monkeypatch, trace):
    import asyncio
    from potluck_local.services import investigations as inv
    from potluck_local.services.evidence_lookup import EvidenceScope

    async def get_item(scope, iid):
        return {'status': 'draft', 'provenance': {'template_version': 'investigation-v12', 'snippet_trace': trace}, 'evidence': [], 'brief': {'answers': []}}
    monkeypatch.setattr(inv, 'get_investigation', get_item)
    with pytest.raises(inv.BriefValidationError):
        asyncio.run(inv.review_investigation(EvidenceScope('w', ('c',)), 'id', 'correct', 'owner', 'review', corrected_brief={'answers': []}))


def test_duplicate_json_keys_are_rejected():
    with pytest.raises(ValueError):
        v12.decode_plan('{"R1":["ready"],"R1":["blocked"]}', PLAN)
    parts, _ = v12.decode_plan('{"R1":["ready"]}', PLAN)
    value = '{"status":"supported","text":"Ready.","snippet_ids":["S1"]}'
    for raw in (
        '{"R1":{"R1P1":' + value + '},"R1":{"R1P1":' + value + '}}',
        '{"R1":{"R1P1":' + value + ',"R1P1":' + value + '}}',
        '{"R1":{"R1P1":{"status":"unknown","status":"supported","text":"Ready.","snippet_ids":["S1"]}}}',
    ):
        with pytest.raises(ValueError):
            v12.decode_results(raw, PLAN, SNIPPETS, [], [], parts)


def test_mixed_plan_keeps_quantity_context(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.services.evidence_lookup import EvidenceScope
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    calls = []
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            calls.append(payload)
            if len(calls) == 1:
                assert set(payload) == {'operations'} and len(payload['operations']) == 1
                data = {'R1': ['ready']}
            else:
                assert 'quantities' in payload and 'extraction_notices' in payload
                assert [q['value'] for q in payload['quantities']] == ['100', '200']
                n1, n2 = payload['quantities']
                sid = n1['snippet_id']
                data = {'R1': {'R1P1': {'status': 'supported', 'text': 'Ready.', 'snippet_ids': [sid]}},
                        'R2': {'status': 'ready', 'reason': 'Source quantities selected.', 'minuend_id': n2['id'], 'subtrahend_id': n1['id'], 'known_input_snippet_ids': [sid], 'competing_snippet_ids': [], 'limitation_snippet_ids': [], 'limitation_basis': 'none'}}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'
        folder.mkdir()
        (folder / 'release.md').write_text('# Release\nPilot ready. Latency changed from 100 to 200 ms.')
        cid = await register_collection('w', 'docs', folder)
        await scan_collection('w', cid)
        plan = [{'request': 'What is ready?', 'operation': 'answer'}, {'request': 'How did latency change?', 'operation': 'difference'}]
        item = await inv.create_investigation(EvidenceScope('w', (cid,)), 'What is ready and how did latency change?', 'release', 'model', template_version='investigation-v12', reviewed_operations=plan)
        assert len(calls) == 2
        assert item['provenance']['quantity_catalog']
        assert item['brief']['answers'][1]['calculation']['result'] == '0.1'
    asyncio.run(run())
