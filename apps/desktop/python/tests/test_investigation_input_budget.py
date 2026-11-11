import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from potluck_local.services import investigation_input_budget as budget
from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.source_collections import register_collection, scan_collection


def test_fit_preserves_whole_passages_schema_and_omission_receipt():
    data = {'snippets': [{'snippet_id': f'S{i}', 'text': str(i) * 300} for i in range(1, 20)],
            'quantities': [{'id': f'N{i}', 'snippet_id': f'S{i}'} for i in range(1, 20)],
            'extraction_notices': [{'snippet_id': 'S19'}], 'passage_text_budget': {'units': [{'snippet_id': 'S19'}]}}
    original = deepcopy(data)
    def build(selected):
        return [{'role': 'user', 'content': json.dumps(selected)}], {'ids': [q['id'] for q in selected['quantities']]}, {}
    async def measure(messages, schema):
        return {'input_tokens': len(messages[0]['content']) // 3, 'context_tokens': 3000}
    messages, schema, _, trace = asyncio.run(budget.fit(data, build, measure))
    sent = json.loads(messages[0]['content'])
    assert data == original and trace['omitted_snippet_ids']
    assert trace['input_tokens'] + 2000 <= 3000 and len(trace['probes']) > 1
    assert sent['snippets'] == original['snippets'][:len(sent['snippets'])]
    assert schema['ids'] == trace['selected_quantity_ids']
    assert not sent['extraction_notices'] and not sent['passage_text_budget']['units']
    assert budget.NOTICE in sent['evidence_notices']
    with pytest.raises(ValueError, match='omitted'):
        budget.validate_selection('{"minuend_id":"N19"}', trace)
    with pytest.raises(ValueError, match='omitted'):
        budget.validate_selection('{"snippet_ids":["S19"]}', trace)


def test_fixed_envelope_overflow_never_generates():
    async def measure(*args): return {'input_tokens': 7000, 'context_tokens': 8192}
    with pytest.raises(ValueError, match='cannot fit'):
        asyncio.run(budget.fit({'snippets': []}, lambda d: ([], {}, {}), measure))


@pytest.mark.parametrize('unknown_mode,presentation_mode', [(None,None),('cited-unknown-v1',None),('cited-unknown-v2',None),('cited-unknown-v3',None),('cited-unknown-v4',None),('cited-unknown-v4','cited-conclusions-v1'),('cited-unknown-v4','cited-conclusions-v2'),('cited-unknown-v4','cited-conclusions-v3'),('cited-unknown-v4','cited-conclusions-v4'),('cited-unknown-v4','cited-conclusions-v5'),('cited-unknown-v4','cited-conclusions-v6'),('cited-unknown-v4','cited-conclusions-v7'),('cited-unknown-v4','cited-components-v1')])
@pytest.mark.parametrize('kind', ['rate', 'unknown', 'list', 'continued_list'])
def test_bounded_service_review_export(tmp_path, monkeypatch, kind, unknown_mode, presentation_mode):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path/'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    measured = []; calls = []
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a'*64
        async def validate_investigation_profile(self, *a, **kw): pass
        async def measure_investigation_prompt(self, messages, **kwargs):
            measured.append(messages)
            return {'input_tokens': 1000, 'context_tokens': 8192}
        async def chat(self, messages, **kwargs):
            calls.append(messages)
            assert messages == measured[-1] and kwargs['expected_context_tokens'] == 8192
            if presentation_mode in ('cited-conclusions-v6', 'cited-conclusions-v7'):
                assert 'Select snippet_ids BEFORE writing prose' in messages[0]['content']
                assert ('Fictional writing example' in messages[0]['content']) == (presentation_mode == 'cited-conclusions-v7')
                assert 'Do not paraphrase or copy source text' not in messages[0]['content']
                assert 'only free-text explanation exception' not in messages[0]['content']
            data = json.loads(messages[1]['content'])
            if kind == 'rate':
                a, b = [q for q in data['quantities'] if q['unit'] == 'token/s']
                value = {'status': 'ready', 'reason': 'Cited values', 'minuend_id': a['id'], 'subtrahend_id': b['id'],
                    'known_input_snippet_ids': list(dict.fromkeys([a['snippet_id'], b['snippet_id']])),
                    'competing_snippet_ids': [], 'limitation_snippet_ids': [], 'limitation_basis': 'none'}
            else:
                ids = [] if kind == 'unknown' else [next(s['snippet_id'] for s in data['snippets'] if '2. useful second' in s['text'])]
                value = {'groups': [{'focus': 'Requested fact', 'snippet_ids': ids, 'status': 'unknown' if kind == 'unknown' else 'supported'}], 'more': False}
            if unknown_mode and kind != 'rate':
                value['groups'][0]['unknown_reason'] = 'The evidence describes a procedure, not the release that first included it.' if kind == 'unknown' else ''
                if kind == 'unknown': value['groups'][0]['snippet_ids'] = [data['snippets'][0]['snippet_id']]
            if presentation_mode and kind != 'rate':
                value['groups'][0]['conclusion'] = '' if kind == 'unknown' else 'Complete the useful second step.'
            if presentation_mode == 'cited-components-v1':
                assert 'answer_component_budget' in data
                assert 'explicitly capture conditions, exceptions and distinctions' in messages[0]['content']
                if kind != 'rate':
                    g = value['groups'][0]
                    g.update(conditions=[], exceptions=[], distinctions=[])
                    field = 'distinctions' if kind == 'unknown' else 'conditions'
                    component_text = 'A procedure description does not identify its first release.' if kind == 'unknown' else 'Follow the documented sequence.'
                    g[field] = [{'text': component_text, 'snippet_ids': g['snippet_ids']}]
            import jsonschema
            jsonschema.validate({'R1': value}, kwargs['response_format']['schema'])
            return {'choices': [{'message': {'content': json.dumps({'R1': value})}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db(); folder=tmp_path/'docs'; folder.mkdir()
        text = '# Useful\n\nThroughput: **146.2 tok/s** and **96.8 tok/s**.\n' if kind == 'rate' else '# Useful\n\n1. useful first\n2. useful second\n3. useful third\n'
        if kind == 'continued_list':
            text = '# Useful\n\n1. useful first\n\n2. useful second ' + 'context '*180 + '\n\n3. useful third ' + 'context '*180
        (folder/'a.md').write_text(text)
        cid = await register_collection('w', 'docs', folder); await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,)); question = 'What exact release first supported useful behavior?' if kind == 'unknown' else 'Explain useful throughput or procedure.'
        item = await inv.create_investigation(scope, question, 'useful', 'model', template_version='investigation-v21',
            reviewed_operations=[{'request': question, 'operation': 'difference' if kind == 'rate' else 'answer'}],
            generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1', reliability_mode=budget.VERSION, unknown_mode=unknown_mode, presentation_mode=presentation_mode)
        answer = item['brief']['answers'][0]; text = answer['text']
        assert len(calls) == 1 and item['provenance']['quantity_parser_version'] == 'v5'
        if kind == 'rate': assert '49.4 token/s' in text
        if kind == 'unknown':
            if unknown_mode: assert 'not the release' in text and 'unverified' in text
            else: assert question in text and 'does not establish' in text
        if kind == 'list': assert 'Structural context may be incomplete' not in text and len(answer['snippet_ids']) == 3
        if kind == 'continued_list': assert 'Structural context may be incomplete' in text
        correction = deepcopy(item['brief'])
        if (unknown_mode and kind == 'unknown') or presentation_mode: correction['answers'][0]['text'] = 'Owner corrected the answer.'
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'Fixture review only', correction)
        assert reviewed['brief'] == item['brief']
        from potluck_local.routers.investigations import export as export_item
        exported = await export_item(item['id'], workspace='w', collection_ids=[cid])
        assert ('Owner corrected' if (unknown_mode and kind == 'unknown') or presentation_mode else '49\\.4' if kind == 'rate' else 'Extractive') in exported['markdown']
        if presentation_mode:
            if presentation_mode == 'cited-components-v1' and kind != 'rate':
                assert 'Original answer components' in exported['markdown']
                assert ('A procedure description' if kind == 'unknown' else 'Follow the documented sequence') in exported['markdown']
            assert reviewed['provenance'] == item['provenance']
            assert 'Original cited conclusions (unverified)' in exported['markdown']
            if kind in ['list','continued_list']:
                assert 'Complete the useful second step' in exported['markdown']
                assert all('Complete the useful second step' not in c['text'] for c in item['brief']['answers'][0]['parts'][0]['claims'])
        if unknown_mode and kind == 'unknown':
            assert 'Original missing-evidence explanations' in exported['markdown']
            original = deepcopy(item['provenance'])
            assert reviewed['provenance'] == original
            exported = await export_item(item['id'],workspace='w',collection_ids=[cid])
            assert 'Owner corrected' in exported['markdown'] and 'not the release' in exported['markdown']
            assert 'Related source (' in exported['markdown']
    asyncio.run(run())


def test_operand_limit_is_applied_after_whole_passage_selection():
    data = {'snippets': [{'snippet_id': 'S1', 'text': 'needed'}, {'snippet_id': 'S2', 'text': 'extra'}],
            'quantities': [{'id': 'N1', 'snippet_id': 'S1'}] + [{'id': f'N{i}', 'snippet_id': 'S2'} for i in range(2, 107)]}
    async def measure(*a): return {'input_tokens': 1000, 'context_tokens': 8192}
    def build(d):
        assert len(d['quantities']) <= 100
        return [], {}, {}
    _, _, _, trace = asyncio.run(budget.fit(data, build, measure))
    assert trace['selected_snippet_ids'] == ['S1'] and trace['selected_quantity_ids'] == ['N1']
    assert trace['omitted_snippet_ids'] == ['S2']
