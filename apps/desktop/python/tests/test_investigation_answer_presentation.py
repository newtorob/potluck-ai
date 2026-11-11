import copy
import json

import pytest

from potluck_local.services import investigation_answer_presentation as p

PLAN = [{'id': 'r1', 'operation': 'answer'}]
GROUPS = [
    {'focus': 'Cost', 'status': 'supported', 'snippet_ids': ['S1']},
    {'focus': 'Date', 'status': 'unknown', 'snippet_ids': []},
    {'focus': 'Scope', 'status': 'conflicting', 'snippet_ids': ['S1']},
]
SNIPPETS = [{'snippet_id': 'S1', 'text': 'A source.'}]


def test_schema_copies_and_orders_field():
    group = {'properties': {k: {} for k in ('focus', 'status', 'unknown_reason', 'snippet_ids')},
             'required': ['focus', 'status', 'unknown_reason', 'snippet_ids']}
    schema = {'properties': {'r1': {'properties': {'groups': {'items': group}}}}}
    original = copy.deepcopy(schema)
    result = p.response_schema(schema, PLAN)['properties']['r1']['properties']['groups']['items']
    assert schema == original
    assert list(result['properties']) == ['focus', 'status', 'snippet_ids', 'unknown_reason', 'conclusion']
    assert result['required'] == ['focus', 'status', 'snippet_ids', 'unknown_reason', 'conclusion']


def test_extract_records_restore_and_render():
    groups = [dict(g, conclusion=t) for g, t in zip(GROUPS, ['  It costs $10.  ', '', ''])]
    stripped, values = p.extract(json.dumps({'r1': {'groups': groups}}), PLAN)
    payload = json.loads(stripped)
    assert values == {'r1': ['It costs $10.', '', '']}
    assert all('conclusion' not in g for g in payload['r1']['groups'])
    saved = p.records(values, {'focus_groups': {'r1': {'groups': GROUPS}}}, SNIPPETS)
    before = copy.deepcopy(saved)
    restored = p.restore(payload, saved)
    assert saved == before and 'conclusion' not in payload['r1']['groups'][0]
    assert [g['conclusion'] for g in restored['r1']['groups']] == values['r1']
    rendered = p.render(saved['operations']['r1'])
    assert 'unverified' in rendered and 'It costs $10.' in rendered
    assert 'Status: unknown' in rendered and 'Status: conflicting' in rendered and '[S1]' in rendered


@pytest.mark.parametrize('text,status', [('', 'supported'), ('incomplete', 'supported'), ('No. [S1]', 'supported'),
    ('No. [n1]', 'supported'), ('Bad\ntext.', 'supported'), ('Bad\u200btext.', 'supported'),
    ('x' * 301, 'supported'), ('No.', 'unknown'), ('No.', 'conflicting'), (None, 'supported')])
def test_reject_hostile_or_invalid_conclusions(text, status):
    with pytest.raises(ValueError):
        p.extract(json.dumps({'r1': {'groups': [{'status': status, 'conclusion': text}]}}), PLAN)


def test_duplicate_keys_rejected():
    with pytest.raises(ValueError):
        p.extract('{"r1":{"groups":[{"status":"supported","conclusion":"Yes.","conclusion":"No."}]}}', PLAN)


@pytest.mark.parametrize('change', ['version', 'index', 'focus', 'status', 'ids', 'keys', 'missing'])
def test_restore_rejects_malformed_or_tampered_trace(change):
    payload = {'r1': {'groups': copy.deepcopy(GROUPS)}}
    saved = p.records({'r1': ['Yes.', '', '']}, {'focus_groups': {'r1': {'groups': GROUPS}}}, SNIPPETS)
    entry = saved['operations']['r1'][0]
    if change == 'version': saved['version'] = 'wrong'
    elif change == 'index': entry['group_index'] = 1
    elif change == 'focus': entry['focus'] = 'Changed'
    elif change == 'status': entry['status'] = 'unknown'
    elif change == 'ids': entry['snippet_ids'] = []
    elif change == 'keys': entry['extra'] = True
    else: saved['operations']['r1'].pop()
    with pytest.raises(ValueError): p.restore(payload, saved)


@pytest.mark.parametrize('version', [v for v in p.SUPPORTED_VERSIONS if v not in p.COMPONENT_VERSIONS])
def test_focus_limit_is_versioned_in_schema_and_saved_records(version):
    group = {'properties': {'focus': {'type': 'string', 'minLength': 1, 'maxLength': 96},
                            'status': {'type': 'string'}, 'snippet_ids': {'type': 'array'},
                            'unknown_reason': {'type': 'string'}},
             'required': ['focus', 'status', 'snippet_ids', 'unknown_reason']}
    original = {'properties': {'r1': {'properties': {'groups': {'items': group}}}}}
    schema = p.response_schema(original, PLAN, version=version)
    limit = p.MAX_FOCUS if version in p.SHORT_LABEL_VERSIONS else 96
    assert schema['properties']['r1']['properties']['groups']['items']['properties']['focus']['maxLength'] == limit
    assert original['properties']['r1']['properties']['groups']['items']['properties']['focus']['maxLength'] == 96
    groups = [dict(GROUPS[0], focus='x' * 60)]
    record = p.records({'r1': ['Yes.']}, {'focus_groups': {'r1': {'groups': groups}}}, SNIPPETS, version=version)
    restored = p.restore({'r1': {'groups': groups}}, record, version=version)
    assert restored['r1']['groups'][0]['focus'] == 'x' * 60
    groups[0]['focus'] += 'x'
    if version in p.SHORT_LABEL_VERSIONS:
        with pytest.raises(ValueError, match='focus label'):
            p.records({'r1': ['Yes.']}, {'focus_groups': {'r1': {'groups': groups}}}, SNIPPETS, version=version)
    else:
        assert p.records({'r1': ['Yes.']}, {'focus_groups': {'r1': {'groups': groups}}}, SNIPPETS, version=version)


@pytest.mark.parametrize('version', [v for v in p.COMPLETE_VERSIONS if v not in p.COMPONENT_VERSIONS])
@pytest.mark.parametrize('operation', ['answer', 'difference'])
def test_complete_contract_keeps_numeric_rules_and_removes_extract_only_conflict(operation, version):
    instruction = p.complete_instruction([{'id': 'R1', 'operation': operation}], version=version)
    assert 'Select snippet_ids BEFORE writing prose' in instruction
    assert 'Do not paraphrase or copy source text' not in instruction
    assert 'only free-text explanation exception' not in instruction
    assert ('after as minuend' in instruction) == (operation == 'difference')
