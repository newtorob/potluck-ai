import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_value_inventory_v2 as inv
from potluck_local.services import investigation_evidence_comparisons as source
from test_investigation_evidence_revision import sample


def prepared(text, question='Which product release?'):
    e = sample(); e['group']['request'] = e['group']['focus'] = question
    src = e['sources'][0]; src['quote'] = text
    src['context']['cited_chunk'] = {'quote': text, 'selected_start': 0, 'selected_end': len(text)}
    return source.prepare(e)


def test_literal_candidate_is_bound_and_matcher_replays():
    p = prepared('The gateway reports protocol revision 7. The product release is not stated.')
    payload = json.loads(inv.messages(p)[1]['content']); assert [x['value'] for x in payload['candidates']] == ['7']
    result = inv.decode('{"classifications":{"C1":"protocol_version"},"type_context_units":[]}', p)
    fact = result['facts'][0]; unit = p['units'][0]['quote']
    assert unit[fact['value_start']:fact['value_end']] == '7'
    match = inv.prepare_match(p, result)
    assert inv.match_decode('{"F1":"different_fact"}', match)['version'] == inv.MATCH_VERSION
    bad = deepcopy(result); bad['facts'][0]['value'] = '8'
    with pytest.raises(ValueError): inv.prepare_match(p, bad)


def test_source_only_request_and_exhaustive_decisions():
    p = prepared('The protocol version v4.2 is active. Identifier NODE-73 exists.')
    q = prepared('The protocol version v4.2 is active. Identifier NODE-73 exists.', 'What is the identifier?')
    assert inv.messages(p) == inv.messages(q) and inv.response_schema(p) == inv.response_schema(q)
    assert [x['value'] for x in json.loads(inv.messages(p)[1]['content'])['candidates']] == ['v4.2', 'NODE-73']
    for raw in ['{"classifications":{"C1":"protocol_version"},"type_context_units":[]}', '{"classifications":{"C1":"protocol_version","C1":"incidental","C2":"incidental"}}', '{"classifications":{"C1":"protocol_version","C2":"other"}}']:
        with pytest.raises(ValueError): inv.decode(raw, p)


def test_empty_and_candidate_limits():
    p = prepared('No version is supplied.')
    assert inv.empty_inventory(p)['facts'] == []
    with pytest.raises(ValueError): inv.empty_inventory(prepared('Version 2 is supplied.'))
    with pytest.raises(ValueError, match='Candidate limit'): inv.messages(prepared(' '.join(f'Version {n} exists.' for n in range(17))))
    with pytest.raises(ValueError, match='fact limit'): inv.decode(json.dumps({'classifications': {f'C{i}':'software_version' for i in range(1,5)}, 'type_context_units': []}), prepared('Version 1. Version 2. Version 3. Version 4.'))


def test_no_substrings_from_paths_hashes_or_status():
    p = prepared('See /api/v7 and https://host/v8. SHA256 is a hash. HTTP status 404 occurred.')
    assert json.loads(inv.messages(p)[1]['content'])['candidates'] == []
    assert inv.request_scope(prepared('A rule applies.', 'Which revision?')) == 'release_version'


def test_dotted_prerelease_and_duplicate_literal_binding():
    p = prepared('Protocol version v1.8.0-beta exists. Another version v1.8.0-beta exists.')
    assert [c['value'] for c in json.loads(inv.messages(p)[1]['content'])['candidates']] == ['v1.8.0-beta', 'v1.8.0-beta']
    with pytest.raises(ValueError, match='Ambiguous'):
        inv.messages(prepared('Protocol versions v3 and v3 are equivalent.'))


def test_outside_scope_rejects_facts_and_mixed_status_is_explicit():
    p = prepared('Protocol version 3 exists.', 'Who approves?')
    with pytest.raises(ValueError, match='Outside value scope'):
        inv.decode('{"classifications":{"C1":"protocol_version"},"type_context_units":[]}', p)
    with pytest.raises(ValueError, match='Ambiguous status'):
        inv.messages(prepared('Protocol version 3 returns HTTP status 404.'))


def test_exact_classification_keys_and_malformed_values():
    p = prepared('Protocol version v4.2 exists. Identifier NODE-73 exists.')
    invalid = [
        '{"classifications":{"C1":"protocol_version"},"type_context_units":[]}',
        '{"classifications":{"C1":"protocol_version","C2":"incidental","C3":"incidental"}}',
        '{"classifications":{"C1":"protocol_version","C2":"incidental","C2":"incidental"}}',
        '{"classifications":{"C1":"protocol_version","C2":null}}',
        '{"classifications":{"C1":"protocol_version","C2":[]}}',
        '{"classifications":{"C1":"protocol_version","C2":"invented"},"extra":1}',
    ]
    for raw in invalid:
        with pytest.raises(ValueError): inv.decode(raw, p)


def test_source_and_inventory_replay_rejects_deep_tampering():
    p = prepared('Protocol version v4.2 exists. Identifier NODE-73 exists.')
    raw = '{"classifications":{"C1":"protocol_version","C2":"entity_identifier"},"type_context_units":[]}'
    result = inv.decode(raw, p)
    for mutate in [
        lambda x: x['facts'][0].update(value='v9'),
        lambda x: x['facts'][0].update(value_start=999),
        lambda x: x['facts'][0].update(type_quote='invented v4.2'),
        lambda x: x['classifications'].update(C1='incidental'),
        lambda x: x['selected'][0].update(quote='invented'),
    ]:
        changed = deepcopy(result); mutate(changed)
        with pytest.raises(ValueError): inv.prepare_match(p, changed)
    forged = deepcopy(p); forged['units'][0]['quote'] = 'Invented v4.2.'
    for operation in (lambda: inv.messages(forged), lambda: inv.response_schema(forged), lambda: inv.decode(raw, forged)):
        with pytest.raises(ValueError): operation()
    match = inv.prepare_match(p, result); match['source_inventory']['facts'][0]['value_end'] += 1
    with pytest.raises(ValueError): inv.match_decode('{"F1":"different_fact","F2":"irrelevant"}', match)


def test_existing_lexical_forms_and_no_embedded_fragments():
    p = prepared('Release 4.3+build exists. Node NODE_91 exists. Revision V12 exists.')
    values = [c['value'] for c in json.loads(inv.messages(p)[1]['content'])['candidates']]
    assert values == ['4.3+build', 'NODE_91', 'V12']
    for value in values:
        assert inv.value_spans.VALUE.fullmatch(value)
    for text in ['Visit https://example.org/v1.8.', 'Read /tmp/NODE-91.', 'See file:v4.2.']:
        assert json.loads(inv.messages(prepared(text))[1]['content'])['candidates'] == []


@pytest.mark.parametrize('text, expected', [
    ('Protocol version v1.8.0-beta. Next sentence.', 'v1.8.0-beta'),
    ('Protocol version v1.8.0-beta.', 'v1.8.0-beta'),
    ('Release v1.2+build.7. Next sentence.', 'v1.2+build.7'),
    ('Release v1.2+build.7.', 'v1.2+build.7'),
])
def test_suffix_sentence_period_is_excluded(text, expected):
    p = prepared(text)
    assert [c['value'] for c in json.loads(inv.messages(p)[1]['content'])['candidates']] == [expected]
    result = inv.decode('{"classifications":{"C1":"software_version"},"type_context_units":[]}', p)
    fact = result['facts'][0]
    assert p['units'][0]['quote'][fact['value_start']:fact['value_end']] == expected


@pytest.mark.parametrize('value', [True, False, [], {}, ['protocol_version'], {'value': 'protocol_version'}])
def test_non_string_classifications_rejected(value):
    p = prepared('Protocol version v1.2 exists.')
    with pytest.raises(ValueError):
        inv.decode(json.dumps({'classifications': {'C1': value}, 'type_context_units': []}), p)


def test_hash_field_names_are_excluded_before_quote_limit():
    text = 'The exe_sha256 field stores a checksum ' + 'without additional numeric identifiers ' * 10 + '.'
    assert len(text) > 240
    assert json.loads(inv.messages(prepared(text))[1]['content'])['candidates'] == []
    for name in ('exe_sha256', 'potluck_model_sha256', 'X-Potluck-Model-SHA256', 'md5'):
        p = prepared(f'The {name} field is a hash and protocol version v2.4 is supported.')
        assert [x['value'] for x in json.loads(inv.messages(p)[1]['content'])['candidates']] == ['v2.4']
    with pytest.raises(ValueError, match='type quote limit'):
        inv.messages(prepared('Version v2.4 is supported ' + 'with contextual details ' * 15 + '.'))


def test_explicit_type_context_is_retained_and_bound():
    p = prepared('Node identifier NODE-73 exists. Cluster identifier CLUSTER-8 exists. Node and cluster identifiers are different types.')
    payload = json.loads(inv.messages(p)[1]['content'])
    assert len(payload['units']) == 3 and all('unit_id' in c for c in payload['candidates'])
    raw = json.dumps({'classifications': {'C1': 'entity_identifier', 'C2': 'entity_identifier'}, 'type_context_units': ['U3']})
    result = inv.decode(raw, p)
    assert result['type_context_units'] == ['U3']
    match = inv.prepare_match(p, result)
    assert 'U3' in inv.match_decode('{"F1":"requested_fact","F2":"different_fact"}', match)['unit_ids']
    for context in [['U1'], ['U3', 'U3'], ['UNKNOWN'], ['U3'] * 5]:
        with pytest.raises(ValueError): inv.decode(json.dumps({'classifications': {'C1': 'entity_identifier', 'C2': 'entity_identifier'}, 'type_context_units': context}), p)
    tampered = deepcopy(result); tampered['type_context_units'] = []
    with pytest.raises(ValueError): inv.prepare_match(p, tampered)


@pytest.mark.parametrize('text', [
    'Protocol version v4.2 exists.',
    'No numeric identifiers are stated.',
    'Node identifier NODE-73 exists. Node and cluster identifiers are different types.',
])
def test_scanner_schema_admitted_by_actual_runtime(text):
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    fmt = {'type': 'json_object', 'schema': inv.response_schema(prepared(text))}
    assert _structured_response_format(object.__new__(SplitModel), fmt) == fmt


def test_duplicate_context_rejected_even_without_schema_uniqueitems():
    p = prepared('Node identifier NODE-73 exists. Node and cluster identifiers are different types.')
    raw = json.dumps({'classifications': {'C1': 'entity_identifier'}, 'type_context_units': ['U2', 'U2']})
    with pytest.raises(ValueError, match='context'):
        inv.decode(raw, p)
