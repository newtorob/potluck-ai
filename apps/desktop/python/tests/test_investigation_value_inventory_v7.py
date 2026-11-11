import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_evidence_comparisons as source
from test_investigation_evidence_revision import sample

from potluck_local.services import investigation_value_inventory_v7 as inv


def prepared(text, question='Which release?'):
    e = sample(); e['group']['request'] = e['group']['focus'] = question
    src = e['sources'][0]; src['quote'] = text
    src['context']['cited_chunk'] = {'quote': text, 'selected_start': 0, 'selected_end': len(text)}
    return source.prepare(e)


def test_exhaustive_context_and_matcher_replay():
    p = prepared('Protocol revision v4 exists. This denotes the wire protocol. The related product release is not stated. Operational acknowledgment is not success.')
    raw = {'classifications': {'C1': 'protocol_version'}, 'context_classifications': {'U2': 'definition_or_function', 'U3': 'explicit_absence', 'U4': 'operational_or_unrelated'}}
    result = inv.decode(json.dumps(raw), p)
    assert result['type_context_units'] == ['U2', 'U3'] and result['context_classifications'] == {'U2': 'typed_value_definition', 'U3': 'explicit_typed_absence', 'U4': 'background'} and inv.raw_response(result) == raw
    m = inv.prepare_match(p, result)
    assert json.loads(inv.match_messages(m)[1]['content'])['facts'][0]['source_type'] == 'protocol_version'
    assert set(inv.match_decode('{"F1":"different_fact"}', m)['unit_ids']) == {'U1', 'U2', 'U3'}
    with pytest.raises(ValueError): inv.match_decode('{"F1":"invalid_fact"}', m)
    tampered = deepcopy(result); tampered['context_classifications']['U3'] = 'background'
    with pytest.raises(ValueError): inv.prepare_match(p, tampered)


def test_context_exact_set_orphan_and_source_binding():
    p = prepared('Version v2 exists. The value is a protocol revision.')
    for d in [{}, {'U2': 'operational_or_unrelated', 'U9': 'operational_or_unrelated'}, {'U1': 'operational_or_unrelated'}, {'U2': 'bad'}, {'U2': []}]:
        with pytest.raises(ValueError): inv.decode(json.dumps({'classifications': {'C1': 'protocol_version'}, 'context_classifications': d}), p)
    with pytest.raises(ValueError): inv.decode('{"classifications":{"C1":"incidental"},"context_classifications":{"U2":"definition_or_function"}}', p)
    with pytest.raises(ValueError): inv.decode('{"classifications":{"C1":"protocol_version"},"context_classifications":{"U2":"operational_or_unrelated","U2":"operational_or_unrelated"}}', p)
    changed = deepcopy(p); changed['units'][0]['quote'] = 'Forged v2.'
    with pytest.raises(ValueError): inv.messages(changed)


def test_limits_empty_and_source_only():
    p = prepared('Version v2 exists. This identifies the wire format.')
    q = prepared('Version v2 exists. This identifies the wire format.', 'Which identifier?')
    assert inv.messages(p) == inv.messages(q) and inv.response_schema(p) == inv.response_schema(q)
    no = prepared('No version is stated. The ledger is separate.')
    empty = inv.empty_inventory(no)
    assert not empty['facts'] and all(v == 'background' for v in empty['context_classifications'].values())
    with pytest.raises(ValueError): inv.empty_inventory(p)
    too_many = prepared('Version v2 exists. ' + ' '.join('Background sentence remains unrelated.' for _ in range(17)))
    with pytest.raises(ValueError, match='Context candidate limit'): inv.messages(too_many)
    four = prepared('Version v2 exists. First context. Second context. Third context. Fourth context.')
    d = {u['id']: 'definition_or_function' for u in four['units'][1:]}
    with pytest.raises(ValueError, match='Too many selected'): inv.decode(json.dumps({'classifications': {'C1': 'software_version'}, 'context_classifications': d}), four)


@pytest.mark.parametrize('text', ['Version v2 exists.', 'No version is stated.', 'Version v2 exists. This denotes the wire protocol.'])
def test_runtime_schema_admission(text):
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    fmt = {'type':'json_object', 'schema':inv.response_schema(prepared(text))}
    assert _structured_response_format(object.__new__(SplitModel), fmt) == fmt


def test_wire_and_canonical_labels_are_distinct():
    p = prepared('Protocol version v2 exists. It governs framing.')
    wire = {'classifications': {'C1': 'protocol_version'}, 'context_classifications': {'U2': 'definition_or_function'}}
    result = inv.decode(json.dumps(wire), p)
    assert result['context_classifications'] == {'U2': 'typed_value_definition'}
    with pytest.raises(ValueError):
        inv.decode(json.dumps({'classifications': wire['classifications'], 'context_classifications': result['context_classifications']}), p)
    with pytest.raises(ValueError): inv.raw_response({'classifications': {}, 'context_classifications': {'U2': 'unknown'}})
    prompt = inv.messages(p)[0]['content']
    assert 'purpose, meaning, scope or function' in prompt and 'runtime health' in prompt
