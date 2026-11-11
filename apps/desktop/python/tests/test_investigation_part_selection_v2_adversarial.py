from copy import deepcopy
import json

import pytest

from potluck_local.services import investigation_part_selection as legacy
from potluck_local.services import investigation_part_selection_v2 as v2
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import reviewed_source_unit_plan as units


def setup():
    question = 'What rule applies, and what exception or missing expiry is specified?'
    lines = ['A permit is required.', 'Urgent work is exempt.', 'An earlier draft says no permit.', 'Expiry is not stated.']
    text = '\n'.join(lines)
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:policy', 'content': text,
                 'sources': [{'citation_id': 'source:policy', 'content': text}]}]
    offset = 0
    entries = []
    for name, line in zip(('rule', 'exception', 'old', 'context'), lines):
        entries.append({'id': name, 'ranges': [[offset, offset + len(line)]],
                        'required_unit_ids': ['exception'] if name == 'rule' else []})
        offset += len(line) + 1
    plans = {'claim:policy': {'source_sha256': units.text_hash(text), 'units': entries}}
    part_plan = parts.prepare(question, ['What rule applies?', 'What expiry is specified?'], origin='fixture_supplied')
    prepared = v2.prepare(question, evidence, part_plan, unit_plans=plans)
    proposal = json.dumps({'P1': ['C1:rule'], 'P2': []})
    return question, evidence, plans, part_plan, prepared, v2.decode_selection(proposal, prepared)


def role(verdict, answer=(), alternative=(), context=()):
    return {'answer_unit_ids': list(answer), 'alternative_unit_ids': list(alternative),
            'context_unit_ids': list(context), 'verdict': verdict}


def test_part_roles_preserve_unknown_context_and_dependencies():
    _, _, _, _, prepared, first = setup()
    raw = json.dumps({'P1': role('supported', ['C1:rule']),
                      'P2': role('unknown', context=['C1:context'])})
    result = v2.decode_review(raw, prepared, first)
    assert result['status'] == 'unknown'
    assert result['parts'][0]['verdict'] == 'supported'
    assert 'C1:exception' in result['parts'][0]['unit_ids']
    assert 'Urgent work is exempt.' in result['text']
    assert 'Expiry is not stated.' in result['text']
    assert result['coverage'] == 'not_verified' and result['needs_review']


def test_conflict_and_unknown_are_independent_and_reused_unit_is_allowed():
    _, _, _, _, prepared, first = setup()
    raw = json.dumps({'P1': role('conflicting', ['C1:rule'], ['C1:old']),
                      'P2': role('unknown', context=['C1:rule'])})
    result = v2.decode_review(raw, prepared, first)
    assert result['status'] == 'conflicting'
    assert 'unresolved' in result['text'].lower()
    assert all(s in result['text'] for s in ('A permit is required.', 'An earlier draft says no permit.', 'Urgent work is exempt.'))
    assert result['parts'][1]['verdict'] == 'unknown'
    # Shape checks do not prove that the example actually answers either question.
    assert result['coverage'] == 'not_verified'


@pytest.mark.parametrize('value', [
    role('supported'), role('supported', ['C1:rule'], ['C1:old']),
    role('unknown', ['C1:rule']), role('unknown', alternative=['C1:old']),
    role('conflicting', ['C1:rule']), role('conflicting', ['C1:rule'], ['C1:rule']),
    role('supported', ['C1:rule', 'C1:rule']), role('supported', ['C1:rule'], context=['C1:rule']),
    role('supported', [True]), role('supported', ['C1:missing']),
    {'answer_unit_ids': ['C1:rule'], 'alternative_unit_ids': [], 'verdict': 'supported'},
    {**role('supported', ['C1:rule']), 'extra': 'x'},
])
def test_review_shape_and_roles_fail_closed(value):
    _, _, _, _, prepared, first = setup()
    with pytest.raises(ValueError):
        v2.decode_review(json.dumps({'P1': value, 'P2': role('unknown')}), prepared, first)


@pytest.mark.parametrize('raw', ['{}', '{"P1":{},"P1":{},"P2":{}}', '{"P1":{},"P2":{},"P3":{}}', '[]', 'not-json'])
def test_missing_duplicate_extra_or_malformed_review_rejected(raw):
    _, _, _, _, prepared, first = setup()
    with pytest.raises(ValueError):
        v2.decode_review(raw, prepared, first)


def test_full_catalog_visible_for_empty_proposal_and_replay_tamper_rejected():
    question, evidence, plans, plan, prepared, first = setup()
    payload = json.loads(v2.review_messages(prepared, first)[1]['content'])
    assert 'C1:old' in json.dumps(payload) and 'C1:context' in json.dumps(payload)
    raw = json.dumps({'P1': role('supported', ['C1:rule']), 'P2': role('unknown')})
    result = v2.decode_review(raw, prepared, first)
    assert v2.validate_result(question, evidence, plan, first['raw'], raw, result, unit_plans=plans) == result
    forged = deepcopy(result)
    forged['text'] = 'Invented'
    with pytest.raises(ValueError):
        v2.validate_result(question, evidence, plan, first['raw'], raw, forged, unit_plans=plans)
    changed = deepcopy(evidence)
    changed[0]['content'] += ' changed'
    with pytest.raises(ValueError):
        v2.validate_result(question, changed, plan, first['raw'], raw, result, unit_plans=plans)
    with pytest.raises(ValueError):
        v2.prepare(question + ' Different?', evidence, plan, unit_plans=plans)


def test_v1_stays_separately_versioned():
    question, evidence, plans, plan, _, _ = setup()
    prepared = legacy.prepare(question, evidence, plan, unit_plans=plans)
    assert prepared['version'] == legacy.VERSION and v2.VERSION != legacy.VERSION
    assert legacy.response_schema(prepared)['properties']['P1']['type'] == 'array'


def test_dependency_closure_and_visible_budget_reject_without_clipping():
    question = 'Which rule?'
    plan = parts.prepare(question, ['Which rule?'], origin='fixture_supplied')
    lines = [f'Rule {i}.' for i in range(17)]
    text = '\n'.join(lines)
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:rules', 'content': text,
                 'sources': [{'citation_id': 'source:rules', 'content': text}]}]
    entries, offset = [], 0
    for i, line in enumerate(lines):
        entries.append({'id': f'r{i}', 'ranges': [[offset, offset + len(line)]],
                        'required_unit_ids': [f'r{i+1}'] if i < 16 else []})
        offset += len(line) + 1
    prepared = v2.prepare(question, evidence, plan, unit_plans={
        'claim:rules': {'source_sha256': units.text_hash(text), 'units': entries}})
    first = v2.decode_selection('{"P1":[]}', prepared)
    with pytest.raises(ValueError):
        v2.decode_review(json.dumps({'P1': role('supported', ['C1:r0'])}), prepared, first)
    evidence[0]['content'] = 'x' * 8000
    prepared = v2.prepare(question, evidence, plan)
    first = v2.decode_selection('{"P1":[]}', prepared)
    with pytest.raises(ValueError):
        v2.decode_review(json.dumps({'P1': role('supported', ['C1:whole'])}), prepared, first)
