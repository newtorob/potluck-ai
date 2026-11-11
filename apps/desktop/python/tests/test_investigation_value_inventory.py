from copy import deepcopy
import json
from pathlib import Path
import pytest
from potluck_local.services import investigation_value_inventory as probe
from potluck_local.services import investigation_evidence_comparisons as source_contract
from test_investigation_evidence_revision import sample


@pytest.fixture
def prepared():
    envelope = sample()
    envelope['single_focus'] = False
    envelope['group'].update(focus='Which software release?', request='Which release and who approves?')
    text = ('Protocol version v4.2 is supported. The component identifier is NODE-73. '
            'Approval is needed. An administrator reviews. Further details follow.')
    src = envelope['sources'][0]; src['quote'] = text
    src['context']['cited_chunk'] = {'quote': text, 'selected_start': 0, 'selected_end': len(text)}
    return source_contract.prepare(envelope)


def replace_unit_source(prepared, text):
    envelope = deepcopy(prepared['original'])
    src = envelope['sources'][0]; src['quote'] = text
    src['context']['cited_chunk'] = {'quote': text, 'selected_start': 0, 'selected_end': len(text)}
    return source_contract.prepare(envelope)


def scan_raw(facts=None, context=None):
    return json.dumps({'facts': facts or [], 'type_context_units': context or []})


def fact(uid='U1', value='v4.2'):
    return {'unit_id': uid, 'value': value}


def inventory(p):
    return probe.decode(scan_raw([fact()], ['U2']), p)


def test_scanner_has_no_question_or_original_state(prepared):
    before = deepcopy(prepared); messages = probe.messages(prepared); payload = json.loads(messages[1]['content'])
    assert not {'question','focus','full_question_context','original','status','verdict','classifications'} & set(payload)
    assert prepared['selection_scope']['full_question'] not in messages[1]['content']
    assert payload['request_scope'] == 'release_version' and prepared == before
    assert 'untrusted data' in messages[0]['content']


def test_valid_scanner_binding_and_matcher_replay(prepared):
    before = deepcopy(prepared); inv = inventory(prepared); out = probe.prepare_match(prepared, inv)
    row = inv['facts'][0]; unit = prepared['units'][0]['quote']
    assert unit[row['value_start']:row['value_end']] == 'v4.2'
    assert unit[row['type_quote_start']:row['type_quote_end']] == prepared['units'][0]['quote']
    assert inv['needs_review'] and inv['input_sha256'] == probe.value_spans.source_contract.common.digest(prepared)
    assert prepared == before
    payload = json.loads(probe.match_messages(out)[1]['content'])
    assert payload['question'] == prepared['selection_scope']['focus']
    assert payload['full_question_context'] == prepared['selection_scope']['full_question']
    assert payload['facts'][0]['id'] == 'F1' and payload['facts'][0]['source'] == prepared['units'][0]
    result = probe.match_decode('{"F1":"different_fact"}', out)
    assert result['comparison_kind'] == 'not_requested_fact' and result['needs_review']
    assert result['type_context_units'] == ['U2'] and result['selected'][0] == prepared['units'][0]
    result['selected'][0]['quote'] = 'changed'; assert prepared == before


def test_empty_inventory_does_not_match(prepared):
    empty = probe.decode(scan_raw(), prepared)
    with pytest.raises(ValueError, match='Empty inventory'): probe.prepare_match(prepared, empty)


@pytest.mark.parametrize('bad', ['fake_quote', 'offset', 'digest', 'selected', 'extra'])
def test_replay_rejects_tampered_inventory(prepared, bad):
    inv = inventory(prepared)
    if bad == 'fake_quote': inv['facts'][0]['type_quote'] = 'forged'
    if bad == 'offset': inv['facts'][0]['value_start'] += 1
    if bad == 'digest': inv['input_sha256'] = 'bad'
    if bad == 'selected': inv['selected'][0]['quote'] = 'changed'
    if bad == 'extra': inv['extra'] = 'forged'
    with pytest.raises(ValueError): probe.prepare_match(prepared, inv)


@pytest.mark.parametrize('response', [
    '{"facts":[],"facts":[],"type_context_units":[]}',
    '{"facts":[],"type_context_units":[],"extra":"forged"}',
    scan_raw([fact(), fact()]), scan_raw([fact(uid='U999')]), scan_raw(context=['U1']),
    scan_raw([{**fact(), 'type_quote':'Fake v4.2'}]), scan_raw([fact()], ['U1','U1']),
])
def test_invalid_scanner_inventory(prepared, response):
    with pytest.raises(ValueError): probe.decode(response, prepared)


def test_scope_and_unit_union_limits(prepared):
    e = deepcopy(prepared['original']); e['group']['focus'] = 'Who approves?'
    p = source_contract.prepare(e)
    assert probe.response_schema(p)['properties']['facts']['maxItems'] == 0
    with pytest.raises(ValueError): probe.decode(scan_raw([fact()]), p)
    ids = [u['id'] for u in prepared['units']]; assert len(ids) >= 5
    with pytest.raises(ValueError): probe.decode(scan_raw([fact()], ids[1:5]), prepared)


@pytest.mark.parametrize('raw', ['{}', '{"F1":"requested_fact","F1":"irrelevant"}',
    '{"F1":"different_fact","F2":"irrelevant"}', '{"F1":"invented"}'])
def test_matcher_requires_exact_classifications(prepared, raw):
    with pytest.raises(ValueError): probe.match_decode(raw, probe.prepare_match(prepared, inventory(prepared)))


def test_matcher_none_drops_irrelevant_context(prepared):
    inp = probe.prepare_match(prepared, inventory(prepared))
    out = probe.match_decode('{"F1":"requested_fact"}', inp)
    assert out['comparison_kind'] == 'none' and out['facts'] == out['selected'] == out['type_context_units'] == []


def test_native_schemas(prepared):
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    inp = probe.prepare_match(prepared, inventory(prepared))
    for schema in (probe.response_schema(prepared), probe.match_response_schema(inp)):
        response = {'type':'json_object','schema':schema}
        assert _structured_response_format(SplitModel.__new__(SplitModel), response) == response


def test_two_fact_match_preserves_exact_source_and_distinguishes(prepared):
    inv = probe.decode(scan_raw([fact(), fact('U2', 'NODE-73')]), prepared)
    inp = probe.prepare_match(prepared, inv)
    out = probe.match_decode('{"F1":"different_fact","F2":"requested_fact"}', inp)
    assert out['comparison_kind'] == 'distinct_entities'
    assert {f['value'] for f in out['facts']} == {'v4.2', 'NODE-73'}
    assert set(out['unit_ids']) == {'U1', 'U2'}
    inp['source_inventory']['facts'][0]['value'] = 'forged'
    with pytest.raises(ValueError): probe.match_decode('{"F1":"different_fact","F2":"requested_fact"}', inp)


def test_source_quote_is_derived_and_cannot_be_retyped(prepared):
    inv=probe.decode(scan_raw([fact()]),prepared)
    assert inv['facts'][0]['type_quote']==prepared['units'][0]['quote']
    assert inv['facts'][0]['type_quote_start']==0
    assert inv['facts'][0]['type_quote_end']==len(prepared['units'][0]['quote'])
    with pytest.raises(ValueError):probe.decode(scan_raw([{**fact(),'type_quote':'Protocol version'}]),prepared)

def test_irrelevant_fact_does_not_return_through_context(prepared):
    p=replace_unit_source(prepared, 'Protocol version v4.2 is supported. Response flag ACK-1 is set. Approval is needed. An administrator reviews. Further details follow.')
    inv=probe.decode(scan_raw([fact(),fact('U2','ACK-1')],['U1','U2','U3']),p)
    out=probe.match_decode('{"F1":"different_fact","F2":"irrelevant"}',probe.prepare_match(p,inv))
    assert out['unit_ids']==['U1','U3'] and out['type_context_units']==['U1','U3']
    assert len(out['facts'])==1 and out['facts'][0]['value']=='v4.2'

def test_derived_context_retains_existing_source_length_and_ambiguity_limits(prepared):
    p=replace_unit_source(prepared, 'Protocol version v4.2 '+('x'*240))
    with pytest.raises(ValueError):probe.decode(scan_raw([fact()]),p)
    p=replace_unit_source(prepared, 'Protocol version v4.2 or v4.2 applies.')
    with pytest.raises(ValueError):probe.decode(scan_raw([fact()]),p)


@pytest.mark.parametrize('where', ['unit_quote', 'offset', 'boolean_offset', 'scope', 'source', 'version'])
def test_tampered_prepared_source_is_rejected_on_all_entry_points(prepared, where):
    p = deepcopy(prepared)
    if where == 'unit_quote': p['units'][0]['quote'] = 'forged'
    if where == 'offset': p['units'][0]['start'] += 1
    if where == 'boolean_offset': p['units'][0]['start'] = False
    if where == 'scope': p['selection_scope']['focus'] = 'changed'
    if where == 'source': p['original']['sources'][0]['quote'] = 'forged'
    if where == 'version': p['version'] = 'other'
    for fn in (probe.messages, probe.response_schema, probe.request_scope):
        with pytest.raises(ValueError): fn(p)
    with pytest.raises(ValueError): probe.decode(scan_raw(), p)


def test_matcher_all_entry_points_reject_changed_prepared(prepared):
    matcher = probe.prepare_match(prepared, inventory(prepared))
    matcher['units'][0]['quote'] = 'forged'
    for fn in (probe.match_messages, probe.match_response_schema):
        with pytest.raises(ValueError): fn(matcher)
    with pytest.raises(ValueError): probe.match_decode('{"F1":"different_fact"}', matcher)
