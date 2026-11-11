import json
from copy import deepcopy

import pytest

from potluck_local.services import investigation_evidence_workflow as workflow
from potluck_local.services import investigation_evidence_coverage_v3 as coverage
from potluck_local.services import investigation_evidence_comparisons as sources
from potluck_local.services import investigation_value_inventory_v2 as inventory
from test_investigation_evidence_revision import sample
from test_investigation_evidence_workflow import Runtime


def prepared(question='Which software release first supported this?'):
    envelope = sample()
    envelope['group'].update(request=question, focus=question)
    text = 'The gateway reports protocol revision 7. The product release is not stated.'
    envelope['sources'][0]['quote'] = text
    envelope['sources'][0]['context']['cited_chunk'] = {
        'quote': text, 'selected_start': 0, 'selected_end': len(text)}
    return workflow.prepare(envelope, version=workflow.V4_VERSION)


def components():
    p = prepared(); context_input = p['context']
    raw = {'verdict': 'not_stated', 'units': {u['id']: 'background' for u in context_input['units']},
           'dependencies': [], 'comparisons': []}
    context = coverage.decode(json.dumps(raw), context_input)
    value_input = sources.prepare(context_input['original'])
    scanned = inventory.decode('{"classifications":{"C1":"protocol_version"},"type_context_units":[]}', value_input)
    matched = inventory.match_decode('{"F1":"different_fact"}', inventory.prepare_match(value_input, scanned))
    return p, context, scanned, matched


def test_v4_revision_seven_is_different_fact_and_source_bound():
    p, context, scanned, matched = components()
    result = workflow.compose(p, context, scanned, matched)
    fact = scanned['facts'][0]
    unit = sources.prepare(p['context']['original'])['units'][0]
    assert unit['quote'][fact['value_start']:fact['value_end']] == '7'
    assert matched['classifications'] == {'F1': 'different_fact'}
    assert result['version'] == workflow.V4_VERSION
    assert result['comparisons'][0]['kind'] == 'not_requested_fact'
    assert result['selected'][0]['quote'] == unit['quote']
    assert result['semantic_coverage_verified'] is False


@pytest.mark.parametrize('missing', ['context', 'inventory', 'match'])
def test_v4_missing_required_stage_rejected(missing):
    p, c, s, m = components()
    values = {'context': c, 'inventory': s, 'match': m}; values[missing] = None
    with pytest.raises(ValueError): workflow.compose(p, values['context'], values['inventory'], values['match'])


def test_v4_requested_fact_cannot_coexist_with_not_stated():
    p, c, s, _ = components(); value_input = sources.prepare(p['context']['original'])
    m = inventory.match_decode('{"F1":"requested_fact"}', inventory.prepare_match(value_input, s))
    with pytest.raises(ValueError, match='conflicts'): workflow.compose(p, c, s, m)


@pytest.mark.parametrize('target,field,value', [
    ('source', 'quote', 'Forged revision 7.'),
    ('inventory', 'value_start', 999),
    ('inventory', 'value_end', 999),
    ('classification', 'C1', 'incidental'),
])
def test_v4_tampering_rejected(target, field, value):
    p, c, s, m = components()
    if target == 'source': p['context']['units'][0][field] = value
    elif target == 'classification': s['classifications'][field] = value
    else: s['facts'][0][field] = value
    with pytest.raises(ValueError): workflow.compose(p, c, s, m)


class V4Runtime(Runtime):
    async def chat(self, messages, **kwargs):
        payload = json.loads(messages[1]['content']); self.calls.append((payload, kwargs))
        if payload['version'] == coverage.VERSION:
            raw = {'verdict': 'not_stated', 'units': {u['id']: 'background' for u in payload['units']},
                   'dependencies': [], 'comparisons': []}
        elif payload['version'] == inventory.VERSION:
            raw = {'classifications': {} if self.mode == 'bad_inventory' else {'C1': 'protocol_version'}, 'type_context_units': []}
        else:
            raw = {'F1': 'different_fact'}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(raw)}}]}


@pytest.mark.asyncio
async def test_v4_outside_scope_skips_inventory_and_match():
    p = prepared('Who approves this?'); runtime = V4Runtime()
    r = await workflow.generate(p, runtime, model='model', model_sha='sha')
    assert r['status'] == 'proposal' and r['generation_calls'] == len(runtime.calls) == 1
    assert r['result']['comparison_skip_reason'] == 'outside_scope'


@pytest.mark.asyncio
async def test_v4_invalid_inventory_fails_without_retry_or_partial_result():
    runtime = V4Runtime('bad_inventory')
    r = await workflow.generate(prepared(), runtime, model='model', model_sha='sha')
    assert r['status'] == 'not_generated' and r['failed_stage'] == 'inventory'
    assert r['generation_calls'] == len(runtime.calls) == 2 and 'result' not in r
    assert r['maximum_generation_calls'] == 3


def test_v4_inventory_schema_requires_every_candidate_and_rejects_omission():
    p, _, _, _ = components(); value_input = sources.prepare(p['context']['original'])
    schema = inventory.response_schema(value_input)
    classifications = schema['properties']['classifications']
    assert set(schema['required']) == {'classifications', 'type_context_units'} and 'C1' in classifications['required']
    with pytest.raises(ValueError): inventory.decode('{"classifications":{},"type_context_units":[]}', value_input)
    with pytest.raises(ValueError): inventory.decode('{}', value_input)
    with pytest.raises(ValueError): inventory.decode('{"classifications":{"C1":"protocol_version"}}', value_input)


@pytest.mark.asyncio
async def test_v4_generation_composes_all_three_bound_stages():
    p = prepared(); before = deepcopy(p); runtime = V4Runtime()
    r = await workflow.generate(p, runtime, model='model', model_sha='sha')
    assert r['status'] == 'proposal' and r['generation_calls'] == len(runtime.calls) == 3
    assert [stage['name'] for stage in r['stages']] == ['context', 'inventory', 'match']
    assert r['result'] == workflow.compose(p, *(stage['result'] for stage in r['stages']))
    assert all(stage['settings']['timeout_seconds'] <= 180 for stage in r['stages'])
    assert p == before


def test_v4_explanatory_type_context_survives_composition():
    envelope = sample()
    envelope['group'].update(request='Which identifier applies?', focus='Which identifier applies?')
    text = 'CL-12 identifies the customer.\n\nNODE-38 identifies a node.\n\nThe node identifier is not the customer identifier.'
    envelope['sources'][0]['quote'] = text
    envelope['sources'][0]['context']['cited_chunk'] = {
        'quote': text, 'selected_start': 0, 'selected_end': len(text)}
    p = workflow.prepare(envelope, version=workflow.V4_VERSION)
    ctx = p['context']; value_input = sources.prepare(ctx['original'])
    assert len(value_input['units']) == 3
    raw = {'verdict': 'source_rule', 'units': {u['id']: ('answer' if u['id'] == 'U1' else 'background') for u in ctx['units']},
           'dependencies': [], 'comparisons': []}
    c = coverage.decode(json.dumps(raw), ctx)
    s = inventory.decode(json.dumps({'classifications': {'C1': 'entity_identifier', 'C2': 'entity_identifier'},
                                     'type_context_units': ['U3']}), value_input)
    m = inventory.match_decode(json.dumps({'F1': 'requested_fact', 'F2': 'different_fact'}),
                               inventory.prepare_match(value_input, s))
    result = workflow.compose(p, c, s, m)
    assert m['type_context_units'] == ['U3']
    assert any(unit['id'] == 'U3' and unit['quote'] == value_input['units'][2]['quote']
               for unit in result['selected'])
    bad = deepcopy(s); bad['type_context_units'] = []
    with pytest.raises(ValueError): workflow.compose(p, c, bad, m)
