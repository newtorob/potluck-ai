from copy import deepcopy
import json
import pytest
from potluck_local.services import investigation_part_selection_v6 as v3
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import reviewed_source_unit_plan as units


def fixture():
    q = 'What rule applies and what expiry is given?'
    text = 'Approval is needed.\nEmergency work is exempt.\nAn older draft says no approval.'
    lines = text.splitlines(); cursor = 0; source = []
    for name, line in zip(('rule', 'exception', 'old'), lines):
        source.append({'id': name, 'ranges': [[cursor, cursor + len(line)]],
                       'required_unit_ids': ['exception'] if name == 'rule' else []})
        cursor += len(line) + 1
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:a', 'content': text,
                 'sources': [{'citation_id': 'source:a', 'content': text}]}]
    plans = {'claim:a': {'source_sha256': units.text_hash(text), 'units': source}}
    plan = parts.prepare(q, ['What rule applies?', 'What expiry is given?'], origin='fixture_supplied')
    prepared = v3.prepare(q, evidence, plan, unit_plans=plans)
    first = v3.decode_selection('{"P1":["C1:rule"],"P2":[]}', prepared)
    return q, evidence, plans, plan, prepared, first


def row(groups=(), context=()):
    value = {}
    for role, group in zip(('answer_a', 'answer_b'), groups):
        for uid in group:
            value[uid] = role
    for uid in context:
        value[uid] = 'context'
    return value


def test_independent_statuses_exception_closure_and_raw_replay():
    q, evidence, plans, plan, prepared, first = fixture()
    raw = json.dumps({'P1': row([['C1:rule']]), 'P2': row([], ['C1:rule'])})
    result = v3.decode_review(raw, prepared, first)
    assert result['status'] == 'unknown' and result['parts'][0]['verdict'] == 'supported'
    assert result['parts'][1]['verdict'] == 'unknown' and result['review_raw'] == raw
    assert 'Emergency work is exempt.' in result['text']
    assert v3.validate_result(q, evidence, plan, first['raw'], raw, result, unit_plans=plans) == result
    changed = deepcopy(result); changed['text'] = 'forged'
    with pytest.raises(ValueError): v3.validate_result(q, evidence, plan, first['raw'], raw, changed, unit_plans=plans)
    changed = deepcopy(evidence); changed[0]['content'] += ' altered'
    with pytest.raises(ValueError): v3.validate_result(q, changed, plan, first['raw'], raw, result, unit_plans=plans)


def test_adjacent_identical_sources_group_without_losing_labels():
    _, _, _, _, prepared, first = fixture()
    result = v3.decode_review(json.dumps({'P1': row([['C1:rule']]), 'P2': row([['C1:rule']])}), prepared, first)
    assert result['text'].count('Approval is needed.') == 1
    assert result['text'].index('P1:') < result['text'].index('P2:')
    assert len(result['parts']) == 2 and all(p['passages'] for p in result['parts'])


def test_conflict_preserves_both_groups_and_no_winner():
    _, _, _, _, prepared, first = fixture()
    result = v3.decode_review(json.dumps({'P1': row([['C1:rule'], ['C1:old']]), 'P2': row()}), prepared, first)
    assert result['status'] == 'conflicting' and 'No authoritative winner' in result['text']
    assert 'An older draft says no approval.' in result['text'] and 'do not establish P2:' in result['text']


@pytest.mark.parametrize('value', [
    {'C1:foreign': 'answer_a'}, {'C1:rule': True}, {'C1:rule': 'unsupported'},
    {'answer_groups': [['C1:rule']]}, [], None,
])
def test_invalid_roles_reject(value):
    _, _, _, _, prepared, first = fixture()
    with pytest.raises(ValueError): v3.decode_review(json.dumps({'P1': value, 'P2': row()}), prepared, first)


def test_duplicate_nested_keys_reject_without_salvage():
    _, _, _, _, prepared, first = fixture()
    with pytest.raises(ValueError):
        v3.decode_review('{"P1":{"C1:rule":"answer_a","C1:rule":"context"},"P2":{}}', prepared, first)


def test_answer_b_only_is_supported_and_assignment_order_is_catalog_order():
    _, _, _, _, prepared, first = fixture()
    raw = '{"P1":{"C1:old":"answer_b"},"P2":{"C1:old":"context","C1:rule":"answer_a"}}'
    result = v3.decode_review(raw, prepared, first)
    assert result['parts'][0]['verdict'] == 'supported'
    assert result['parts'][0]['answer_unit_ids'] == ['C1:old']
    assert result['parts'][0]['alternative_unit_ids'] == []
    assert result['parts'][1]['requested_unit_ids'] == ['C1:rule', 'C1:old']


def test_duplicate_keys_and_runtime_schema_admission_without_model():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    _, _, _, _, prepared, first = fixture()
    assert _structured_response_format(object.__new__(SplitModel), {'type': 'json_object', 'schema': v3.review_schema(prepared, first)})
    with pytest.raises(ValueError): v3.decode_review('{"P1":{},"P1":{},"P2":{}}', prepared, first)


def test_overflow_fails_without_truncation():
    q = 'What rule?'; plan = parts.prepare(q, [q], origin='fixture_supplied')
    text = 'x' * 8000
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:a', 'content': text,
                 'sources': [{'citation_id': 'source:a', 'content': text}]}]
    prepared = v3.prepare(q, evidence, plan)
    first = v3.decode_selection('{"P1":[]}', prepared)
    with pytest.raises(ValueError, match='budget|limit'):
        v3.decode_review(json.dumps({'P1': row([['C1:whole']])}), prepared, first)


def test_first_catalog_and_legacy_version_tampering_rejects():
    from potluck_local.services import investigation_part_selection as v1
    from potluck_local.services import investigation_part_selection_v2 as v2
    from potluck_local.services import investigation_part_selection_v3 as prior_v3
    from potluck_local.services import investigation_part_selection_v4 as prior_v4
    from potluck_local.services import investigation_part_selection_v5 as prior_v5
    question, evidence, plans, plan, prepared, first = fixture()
    for prior in (v1, v2, prior_v3, prior_v4, prior_v5):
        old = prior.prepare(question, evidence, plan, unit_plans=plans)
        with pytest.raises(ValueError): v3.messages(old)
    forged = deepcopy(prepared)
    forged['catalog']['units'][0]['text'] = 'Changed.'
    with pytest.raises(ValueError): v3.messages(forged)
    forged_first = deepcopy(first)
    forged_first['context_unit_ids'] = []
    with pytest.raises(ValueError): v3.review_messages(prepared, forged_first)
    with pytest.raises(ValueError): v3.prepare(question + ' changed', evidence, plan, unit_plans=plans)


def test_dependency_closure_overflow_rejects():
    q = 'Which rule?'; plan = parts.prepare(q, [q], origin='fixture_supplied')
    lines = [f'Rule {i}.' for i in range(17)]; text = '\n'.join(lines)
    entries = []; offset = 0
    for i, line in enumerate(lines):
        entries.append({'id': f'r{i}', 'ranges': [[offset, offset + len(line)]],
                        'required_unit_ids': [f'r{i+1}'] if i < 16 else []})
        offset += len(line) + 1
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:a', 'content': text,
                 'sources': [{'citation_id': 'source:a', 'content': text}]}]
    prepared = v3.prepare(q, evidence, plan, unit_plans={
        'claim:a': {'source_sha256': units.text_hash(text), 'units': entries}})
    first = v3.decode_selection('{"P1":[]}', prepared)
    with pytest.raises(ValueError, match='closed unit limit'):
        v3.decode_review(json.dumps({'P1': row([['C1:r0']])}), prepared, first)
