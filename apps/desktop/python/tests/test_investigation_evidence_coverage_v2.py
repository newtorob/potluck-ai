from copy import deepcopy
import json
import pytest

from potluck_local.services import investigation_evidence_coverage as v1
from potluck_local.services import investigation_evidence_coverage_v2 as v2
from test_investigation_evidence_revision import sample


def source(focus='Can a member apply?', *, single=True):
    envelope = sample(); envelope['single_focus'] = single
    envelope['group'].update(focus=focus, request=focus if single else 'What applies and who approves?')
    return envelope


def response(prepared, verdict='no'):
    return {'verdict': verdict, 'units': {u['id']: ('answer' if i == 0 else 'background')
            for i,u in enumerate(prepared['units'])}, 'dependencies': [], 'comparisons': []}


def test_policy_no_preserved_and_v1_unchanged():
    envelope = source(); one, two = v1.prepare(envelope), v2.prepare(envelope)
    before = deepcopy(two); raw = json.dumps(response(two))
    a, b = v1.decode(raw, one), v2.decode(raw, two)
    assert a['verdict'] == b['verdict'] == 'no' and b['needs_review'] is True
    assert b['input_sha256'] == v1.previous.v1.common.digest(two) and two == before
    assert v1.response_schema(one)['properties']['comparisons']['maxItems'] > 0
    assert v2.response_schema(two)['properties']['comparisons']['maxItems'] == 0
    assert v2.POLICY_INSTRUCTION in v2.messages(two)[0]['content']


def test_policy_comparison_is_rejected_not_removed():
    p = v2.prepare(source()); data = response(p, 'not_stated')
    data['comparisons'] = [{'kind':'not_requested_fact','unit_ids':[p['units'][0]['id']]}]
    raw = json.dumps(data)
    with pytest.raises(ValueError): v2.decode(raw, p)
    legacy = v1.prepare(source())
    assert v1.decode(raw, legacy)['comparisons']


def test_technical_scope_preserves_v1_schema_and_comparison():
    envelope = source('Which software release?'); one, two = v1.prepare(envelope), v2.prepare(envelope)
    data = response(two, 'not_stated'); data['comparisons'] = [{'kind':'not_requested_fact','unit_ids':[two['units'][0]['id']]}]
    result = v2.decode(json.dumps(data), two)
    assert result['comparisons'] and result['version'] == v2.VERSION
    assert v2.response_schema(two) == v1.response_schema(one)
    assert v2.messages(two)[0] == v1.messages(one)[0]
    a, b = json.loads(v1.messages(one)[1]['content']), json.loads(v2.messages(two)[1]['content'])
    a.pop('version'); b.pop('version'); assert a == b


@pytest.mark.parametrize('change', ['offset', 'scope', 'source', 'version', 'bool_offset'])
def test_modified_input_rejected(change):
    p = v2.prepare(source())
    if change == 'offset': p['units'][0]['start'] += 1
    if change == 'bool_offset': p['units'][0]['start'] = False
    if change == 'scope': p['selection_scope']['focus'] = 'Which version?'
    if change == 'source': p['units'][0]['quote'] = 'forged'
    if change == 'version': p['version'] = v1.VERSION
    for fn in (v2.messages, v2.response_schema):
        with pytest.raises(ValueError): fn(p)
    with pytest.raises(ValueError): v2.decode(json.dumps(response(p)), p)


def test_draft_absent_and_schema_admitted():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    for envelope in (source(), source('Which identifier?')):
        p = v2.prepare(envelope)
        assert 'PRIVATE DRAFT' not in json.dumps(v2.messages(p))
        fmt = {'type': 'json_object', 'schema': v2.response_schema(p)}
        assert _structured_response_format(SplitModel.__new__(SplitModel), fmt) == fmt


def test_malformed_and_duplicate_json_rejected():
    p = v2.prepare(source())
    for raw in ('{}', '{"comparisons":[],"comparisons":[]}', 'null'):
        with pytest.raises(ValueError): v2.decode(raw, p)
