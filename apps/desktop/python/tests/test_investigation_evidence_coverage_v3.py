from copy import deepcopy
import json
import pytest

from potluck_local.services import investigation_evidence_coverage_v2 as v2
from potluck_local.services import investigation_evidence_coverage_v3 as v3
from test_investigation_evidence_revision import sample


def source(focus='What is the procedure?', *, single=True):
    envelope = sample(); envelope['single_focus'] = single
    envelope['group'].update(focus=focus, request=focus if single else 'What is the procedure and who approves?')
    return envelope


def response(p, verdict='source_rule'):
    return {'verdict': verdict, 'units': {u['id']: ('answer' if i == 0 else 'background')
            for i, u in enumerate(p['units'])}, 'dependencies': [], 'comparisons': []}


def test_v3_only_appends_guidance_and_keeps_source_scope_schema():
    envelope = source(single=False); before = deepcopy(envelope)
    one, two = v2.prepare(envelope), v3.prepare(envelope)
    a, b = v2.messages(one), v3.messages(two)
    assert b[0]['content'] == a[0]['content'] + v3.COVERAGE_INSTRUCTION
    assert 'explicit negative or unimplemented' in b[0]['content']
    assert 'each requested part' in b[0]['content'] and 'headings and list introductions' in b[0]['content']
    x, y = json.loads(a[1]['content']), json.loads(b[1]['content'])
    assert x.pop('version') == v2.VERSION and y.pop('version') == v3.VERSION and x == y
    assert v3.response_schema(two) == v2.response_schema(one)
    assert 'PRIVATE DRAFT' not in json.dumps(b) and envelope == before


def test_policy_gate_and_v2_unverified_result_preserved():
    one, two = v2.prepare(source()), v3.prepare(source()); raw = json.dumps(response(two))
    a, b = v2.decode(raw, one), v3.decode(raw, two)
    assert b['version'] == v3.VERSION and b['needs_review'] is True
    assert b['input_sha256'] == v3.sources.common.digest(two)
    assert b.pop('structural_context_units') == []
    a['version'] = b['version']; a['input_sha256'] = b['input_sha256']; assert a == b
    data = response(two, 'not_stated'); data['comparisons'] = [{'kind': 'not_requested_fact', 'unit_ids': ['U1']}]
    with pytest.raises(ValueError): v3.decode(json.dumps(data), two)


def test_technical_comparison_retained_without_certifying_truth():
    p = v3.prepare(source('Which software release?')); data = response(p, 'not_stated')
    data['comparisons'] = [{'kind': 'not_requested_fact', 'unit_ids': ['U1']}]
    result = v3.decode(json.dumps(data), p)
    assert result['comparisons'] and result['needs_review'] is True
    assert result['selected'][0]['quote'] == p['units'][0]['quote']


@pytest.mark.parametrize('change', ['offset', 'bool_offset', 'scope', 'source', 'version'])
def test_tampered_input_rejected(change):
    p = v3.prepare(source())
    if change == 'offset': p['units'][0]['start'] += 1
    if change == 'bool_offset': p['units'][0]['start'] = False
    if change == 'scope': p['selection_scope']['focus'] = 'Other'
    if change == 'source': p['units'][0]['quote'] = 'forged'
    if change == 'version': p['version'] = v2.VERSION
    for fn in (v3.messages, v3.response_schema):
        with pytest.raises(ValueError): fn(p)
    with pytest.raises(ValueError): v3.decode(json.dumps(response(p)), p)


@pytest.mark.parametrize('raw', ['{}', '{"comparisons":[],"comparisons":[]}', 'null', ' '*14001])
def test_malformed_output_rejected(raw):
    with pytest.raises(ValueError): v3.decode(raw, v3.prepare(source()))


def test_native_schema_admission_for_both_scope_modes():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    for envelope in (source(), source('Which identifier?')):
        fmt = {'type':'json_object', 'schema':v3.response_schema(v3.prepare(envelope))}
        assert _structured_response_format(SplitModel.__new__(SplitModel), fmt) == fmt
