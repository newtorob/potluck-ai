import importlib.util
import json
from copy import deepcopy
from pathlib import Path
import pytest
from potluck_local.services import investigation_evidence_comparisons as source
from test_investigation_evidence_revision import sample

path = Path(__file__).with_name('investigation_value_inventory_v5.py')
if path.exists():
    spec = importlib.util.spec_from_file_location('potluck_local.services.investigation_value_inventory_v5', path)
    inv = importlib.util.module_from_spec(spec); spec.loader.exec_module(inv)
else:
    from potluck_local.services import investigation_value_inventory_v5 as inv


def prepared(text, question='Which release?'):
    e = sample(); e['group']['request'] = e['group']['focus'] = question
    src = e['sources'][0]; src['quote'] = text
    src['context']['cited_chunk'] = {'quote': text, 'selected_start': 0, 'selected_end': len(text)}
    return source.prepare(e)


def test_exhaustive_context_and_matcher_replay():
    p = prepared('Protocol revision v4 exists. This denotes the wire protocol. The related product release is not stated. Operational acknowledgment is not success.')
    raw = {'classifications': {'C1': 'protocol_version'}, 'context_classifications': {'U2': 'typed_value_definition', 'U3': 'explicit_typed_absence', 'U4': 'background'}}
    result = inv.decode(json.dumps(raw), p)
    assert result['type_context_units'] == ['U2', 'U3'] and result['context_classifications'] == raw['context_classifications']
    m = inv.prepare_match(p, result)
    assert json.loads(inv.match_messages(m)[1]['content'])['facts'][0]['source_type'] == 'protocol_version'
    assert set(inv.match_decode('{"F1":"different_fact"}', m)['unit_ids']) == {'U1', 'U2', 'U3'}
    with pytest.raises(ValueError): inv.match_decode('{"F1":"invalid_fact"}', m)
    tampered = deepcopy(result); tampered['context_classifications']['U3'] = 'background'
    with pytest.raises(ValueError): inv.prepare_match(p, tampered)


def test_context_exact_set_orphan_and_source_binding():
    p = prepared('Version v2 exists. The value is a protocol revision.')
    for d in [{}, {'U2': 'background', 'U9': 'background'}, {'U1': 'background'}, {'U2': 'bad'}, {'U2': []}]:
        with pytest.raises(ValueError): inv.decode(json.dumps({'classifications': {'C1': 'protocol_version'}, 'context_classifications': d}), p)
    with pytest.raises(ValueError): inv.decode('{"classifications":{"C1":"incidental"},"context_classifications":{"U2":"typed_value_definition"}}', p)
    with pytest.raises(ValueError): inv.decode('{"classifications":{"C1":"protocol_version"},"context_classifications":{"U2":"background","U2":"background"}}', p)
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
    d = {u['id']: 'typed_value_definition' for u in four['units'][1:]}
    with pytest.raises(ValueError, match='Too many selected'): inv.decode(json.dumps({'classifications': {'C1': 'software_version'}, 'context_classifications': d}), four)


@pytest.mark.parametrize('text', ['Version v2 exists.', 'No version is stated.', 'Version v2 exists. This denotes the wire protocol.'])
def test_runtime_schema_admission(text):
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    fmt = {'type':'json_object', 'schema':inv.response_schema(prepared(text))}
    assert _structured_response_format(object.__new__(SplitModel), fmt) == fmt
