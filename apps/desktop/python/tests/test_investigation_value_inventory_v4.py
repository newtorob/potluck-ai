import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_value_inventory_v4 as inv
from test_investigation_value_inventory_v2 import prepared


def inventory():
    p = prepared('Node identifier NODE-73 exists. Cluster identifier CLUSTER-8 exists. Node and cluster identifiers are different types.')
    raw = json.dumps({'classifications': {'C1': 'entity_identifier', 'C2': 'entity_identifier'}, 'type_context_units': ['U3']})
    return p, inv.decode(raw, p)


def test_typed_facts_remain_for_each_binary_relation():
    p, scanned = inventory(); match = inv.prepare_match(p, scanned)
    payload = json.loads(inv.match_messages(match)[1]['content'])
    assert [f['source_type'] for f in payload['facts']] == ['entity_identifier', 'entity_identifier']
    result = inv.match_decode('{"F1":"requested_fact","F2":"different_fact"}', match)
    assert {f['value'] for f in result['facts']} == {'NODE-73', 'CLUSTER-8'} and 'U3' in result['unit_ids']
    all_requested = inv.match_decode('{"F1":"requested_fact","F2":"requested_fact"}', match)
    assert len(all_requested['facts']) == 2
    with pytest.raises(ValueError, match='Invalid source fact'):
        inv.match_decode('{"F1":"requested_fact","F2":"invalid_fact"}', match)
    with pytest.raises(ValueError): inv.match_decode('{"F1":"requested_fact","F2":"irrelevant"}', match)


def test_source_type_is_derived_from_replayed_model_classification():
    p, scanned = inventory(); changed = deepcopy(scanned)
    changed['classifications']['C1'] = 'protocol_version'
    match = inv.prepare_match(p, changed)
    assert json.loads(inv.match_messages(match)[1]['content'])['facts'][0]['source_type'] == 'protocol_version'
    match['source_inventory']['classifications']['C1'] = 'incidental'
    with pytest.raises(ValueError): inv.match_messages(match)
    changed = deepcopy(scanned); changed['facts'][0]['value'] = 'FORGED-9'
    with pytest.raises(ValueError): inv.prepare_match(p, changed)


def test_scanner_prompt_source_only_and_narrow_type_context():
    p, _ = inventory(); q = prepared('Node identifier NODE-73 exists. Cluster identifier CLUSTER-8 exists. Node and cluster identifiers are different types.', 'Which release?')
    assert inv.messages(p) == inv.messages(q) and inv.response_schema(p) == inv.response_schema(q)
    prompt = inv.messages(p)[0]['content']
    assert 'explicit distinctions' in prompt and 'acknowledgment success or failure' in prompt
    assert 'question' not in json.loads(inv.messages(p)[1]['content'])


def test_runtime_accepts_scanner_and_matcher_schemas():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    p, scanned = inventory()
    for schema in (inv.response_schema(p), inv.match_response_schema(inv.prepare_match(p, scanned))):
        fmt = {'type': 'json_object', 'schema': schema}
        assert _structured_response_format(object.__new__(SplitModel), fmt) == fmt
