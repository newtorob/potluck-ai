from copy import deepcopy
import json
import pytest
from potluck_local.services import investigation_evidence_dependencies as dep
from test_investigation_evidence_revision import sample


def prepared():
    value = sample(); value['single_focus'] = True
    value['group']['request'] = 'What is the software release?'
    text = 'The protocol version is two. Approval is required. Directors may defer requests.'
    source = value['sources'][0]; source['quote'] = text
    source['context']['cited_chunk'] = {'quote': text, 'selected_start': 0, 'selected_end': len(text)}
    return dep.prepare(value)


def answer(p):
    return {'verdict': 'not_stated', 'units': {u['id']: 'background' for u in p['units']},
            'dependencies': [], 'distinctions': []}


def test_transitive_cycle_retains_exact_dependencies():
    p = prepared(); raw = answer(p); raw['units']['U3'] = 'exception'
    raw['dependencies'] = [{'unit_id': 'U3', 'requires': ['U2']}, {'unit_id': 'U2', 'requires': ['U1']},
                           {'unit_id': 'U1', 'requires': ['U3']}]
    out = dep.decode(json.dumps(raw), p)
    assert [u['classification'] for u in out['selected']] == ['dependency', 'dependency', 'exception']
    assert [u['quote'] for u in out['selected']] == [u['quote'] for u in p['units']]
    assert out['needs_review'] is True and out['dependencies'] == raw['dependencies']


@pytest.mark.parametrize('edges', [
    [{'unit_id': 'U2', 'requires': ['U1']}],
    [{'unit_id': 'U1', 'requires': ['U1']}],
    [{'unit_id': 'U1', 'requires': ['U9']}],
    [{'unit_id': 'U1', 'requires': ['U2', 'U2']}],
    [{'unit_id': 'U1', 'requires': ['U2']}, {'unit_id': 'U1', 'requires': ['U3']}],
    [{'unit_id': 'U1', 'requires': []}],
    [{'unit_id': 'U1', 'requires': ['U2']}] * 5,
])
def test_invalid_or_disconnected_links_rejected(edges):
    p = prepared(); raw = answer(p); raw['units']['U1'] = 'answer'; raw['dependencies'] = edges
    with pytest.raises(ValueError): dep.decode(json.dumps(raw), p)


def test_not_stated_requires_evidence_and_exact_distinction_literals():
    p = prepared(); raw = answer(p)
    with pytest.raises(ValueError, match='related evidence'): dep.decode(json.dumps(raw), p)
    raw['distinctions'] = [{'unit_id': 'U1', 'value': 'two', 'actual_type': 'protocol version', 'requested_type': 'software release'}]
    result = dep.decode(json.dumps(raw), p)
    assert result['selected'][0]['classification'] == 'distinction'
    assert result['distinctions'][0]['value'] == 'two'
    for field, invalid in [('value', '99'), ('actual_type', 'release'), ('requested_type', 'fake')]:
        bad = deepcopy(raw); bad['distinctions'][0][field] = invalid
        with pytest.raises(ValueError): dep.decode(json.dumps(bad), p)
    raw['distinctions'].append(deepcopy(raw['distinctions'][0]))
    with pytest.raises(ValueError): dep.decode(json.dumps(raw), p)


def test_nonbinary_and_partial_group_restrict_verdicts_and_scope():
    p = prepared(); raw = answer(p); raw['units']['U1'] = 'answer'; raw['verdict'] = 'yes'
    with pytest.raises(ValueError): dep.decode(json.dumps(raw), p)
    e = sample(); e['single_focus'] = False; e['group']['request'] = 'Can someone apply?'; e['group']['focus'] = 'Exceptions'
    p = dep.prepare(e); assert p['selection_scope']['kind'] == 'selected_group'
    assert p['selection_scope']['focus'] == 'Exceptions' and p['selection_scope']['allowed_verdicts'] == ['source_rule', 'not_stated']
    raw = answer(p); raw['units']['U1'] = 'answer'; raw['verdict'] = 'no'
    with pytest.raises(ValueError): dep.decode(json.dumps(raw), p)
    assert json.loads(dep.messages(p)[1]['content'])['question'] == 'Exceptions'


def test_input_offsets_and_schema_admission():
    inp = sample(); inp['sources'][0]['context']['cited_chunk']['selected_start'] = 0
    with pytest.raises(ValueError): dep.prepare(inp)
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    response = {'type': 'json_object', 'schema': dep.response_schema(prepared())}
    assert _structured_response_format(SplitModel.__new__(SplitModel), response) == response
