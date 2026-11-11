import json

import pytest

from potluck_local.services import investigation_calculations as calc
from potluck_local.services import investigation_explicit_operations as ops
from potluck_local.services import investigation_reviewed_operations as v9


@pytest.fixture
def case():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Alpha 10 ms. Beta 20 ms.'},
                {'snippet_id': 'S2', 'citation_id': 'C2', 'quote': 'Beta 30 ms; authority is disputed.'}]
    quantities = calc.quantity_catalog(snippets, version='v3')
    plan = v9.plan_with_ids([{'request': 'Alpha minus beta?', 'operation': 'difference'}])
    row = {'status': 'ready', 'minuend_id': 'N1', 'subtrahend_id': 'N2',
           'known_input_snippet_ids': ['S1'], 'competing_snippet_ids': [],
           'limitation_snippet_ids': [], 'limitation_basis': 'none', 'reason': 'Selected values.'}
    return snippets, quantities, plan, row


def decode(case):
    snippets, quantities, plan, row = case
    return ops.decode_results(json.dumps({'R1': row}), plan, snippets, quantities, [], version='v3')


def test_named_roles_control_signed_non_temporal_difference(case):
    brief, traces = decode(case)
    assert traces[0]['result'] == '-0.01'
    assert traces[0]['operand_roles'] == {'minuend_id': 'N1', 'subtrahend_id': 'N2'}
    assert brief['answers'][0]['calculation'] == traces[0]


@pytest.mark.parametrize('change', [
    {'minuend_id': 'unavailable'}, {'minuend_id': 'N999'}, {'minuend_id': 'N2'},
    {'known_input_snippet_ids': []}, {'limitation_basis': 'documented'},
    {'status': []}, {'status': 'missing'}, {'status': 'unsupported'},
    {'status': 'conflicting', 'limitation_basis': 'not_established'},
    {'competing_snippet_ids': ['S2']}, {'limitation_snippet_ids': ['S2']},
])
def test_invalid_roles_or_documented_limitation_rejected(case, change):
    case[3].update(change)
    with pytest.raises(ValueError, match='Invalid model format'):
        decode(case)


def test_conflicting_and_limitation_evidence_preserved(case):
    case[3].update(status='conflicting', competing_snippet_ids=['S2'],
                   limitation_snippet_ids=['S2'], limitation_basis='documented',
                   reason='Authority is disputed.')
    brief, traces = decode(case)
    assert traces[0]['status'] == 'conflicting' and traces[0]['result'] is None
    assert traces[0]['competing_snippet_ids'] == ['S2']
    assert brief['answers'][0]['snippet_ids'] == ['S1', 'S2']


def test_missing_role_can_remain_unestablished(case):
    case[3].update(status='missing', minuend_id='unavailable', limitation_basis='not_established')
    _, traces = decode(case)
    assert traces[0]['operand_roles']['minuend_id'] is None
    assert traces[0]['limitation_basis'] == 'not_established'


def test_schema_compiles_with_installed_grammar_for_populated_and_empty_catalog(case):
    llama_cpp = pytest.importorskip('llama_cpp')
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    model = SplitModel.__new__(SplitModel)
    snippets, quantities, plan, _ = case
    for source, numbers in ((snippets, quantities), ([], [])):
        schema = ops.response_schema(plan, source, numbers)
        response = {'type': 'json_object', 'schema': schema}
        assert _structured_response_format(model, response) == response
        assert llama_cpp.LlamaGrammar.from_json_schema(json.dumps(schema), verbose=False)


def test_create_v10_persists_explicit_trace(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.services.evidence_lookup import EvidenceScope
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            assert kwargs['bounded_generation'] and kwargs['max_tokens'] == 2000
            assert 'minuend_id' in kwargs['response_format']['schema']['properties']['R1']['properties']
            numbers = payload['quantities']
            data = {'R1': {'status': 'ready', 'minuend_id': numbers[0]['id'],
                           'subtrahend_id': numbers[1]['id'],
                           'known_input_snippet_ids': list(dict.fromkeys(q['snippet_id'] for q in numbers[:2])),
                           'competing_snippet_ids': [], 'limitation_snippet_ids': [],
                           'limitation_basis': 'none', 'reason': 'Selected values.'}}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'
        folder.mkdir()
        (folder / 'release.md').write_text('# Release\nAlpha 10 ms. Beta 20 ms.')
        cid = await register_collection('w', 'docs', folder)
        await scan_collection('w', cid)
        item = await inv.create_investigation(EvidenceScope('w', (cid,)), 'Alpha minus beta?',
            'release', 'model', template_version='investigation-v10',
            reviewed_operations=[{'request': 'Alpha minus beta?', 'operation': 'difference'}])
        provenance = item['provenance']
        assert provenance['template_version'] == 'investigation-v10'
        assert provenance['quantity_parser_version'] == 'v3'
        assert provenance['calculation_traces'][0]['result'] == '-0.01'
        assert provenance['calculation_traces'][0]['operand_roles']['minuend_id'] == 'N1'
        assert item['brief']['answers'][0]['calculation'] == provenance['calculation_traces'][0]
    asyncio.run(run())
