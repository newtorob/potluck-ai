from copy import deepcopy
import json

import pytest

from potluck_local.services import investigation_part_selection as selection
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import reviewed_source_unit_plan as units


def fixture():
    question = 'Under the archived policy, what is required and when does it expire?'
    statements = ['Written approval is required.', 'Emergency work is exempt.',
                  'Deployment remains deferred pending verification.']
    text = '\n'.join(statements)
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:policy', 'content': text,
                 'sources': [{'citation_id': 'source:policy', 'content': text}]}]
    local_units = []
    cursor = 0
    for uid, statement in zip(('rule', 'exception', 'deferred'), statements):
        local_units.append({'id': uid, 'ranges': [[cursor, cursor + len(statement)]],
                            'required_unit_ids': ['exception'] if uid == 'rule' else []})
        cursor += len(statement) + 1
    plans = {'claim:policy': {'source_sha256': units.text_hash(text), 'units': local_units}}
    plan = parts.prepare(question, ['What is required?', 'When does it expire?'],
                         origin='reviewer_supplied')
    prepared = selection.prepare(question, evidence, plan, unit_plans=plans)
    raw = json.dumps({'P2': [], 'P1': ['C1:rule']})
    first = selection.decode_selection(raw, prepared)
    return question, evidence, plans, plan, prepared, first


def reply(first=None, second=None):
    return json.dumps({'P2': second or {'verdict': 'unknown', 'unit_ids': []},
                       'P1': first or {'verdict': 'supported', 'unit_ids': ['C1:rule']}})


def test_fixed_keys_allow_reply_order_without_omitting_unknown_or_exceptions():
    _, _, _, _, prepared, first = fixture()
    result = selection.decode_review(reply(), prepared, first)
    assert [p['id'] for p in result['parts']] == ['P1', 'P2']
    assert result['status'] == 'unknown'
    assert result['parts'][0]['dependency_only_unit_ids'] == ['C1:exception']
    assert result['source_text'] == 'Written approval is required.\nEmergency work is exempt.'
    assert result['parts'][1]['source_text'] == ''
    assert 'selected reviewed statements do not establish P2: When does it expire?' in result['text']
    assert result['coverage'] == 'not_verified' and result['needs_review'] is True
    assert result['unit_ids'] == ['C1:rule', 'C1:exception']
    assert 'whole document' not in result['text']


def test_explicit_negative_and_deferred_statements_can_be_supported():
    _, _, _, _, prepared, first = fixture()
    result = selection.decode_review(reply(
        {'verdict': 'supported', 'unit_ids': ['C1:exception']},
        {'verdict': 'supported', 'unit_ids': ['C1:deferred']}), prepared, first)
    assert result['status'] == 'supported'
    assert 'Emergency work is exempt.' in result['source_text']
    assert 'Deployment remains deferred pending verification.' in result['source_text']
    # Structural support cannot prove that these facts answer the supplied questions.
    assert result['coverage'] == 'not_verified' and result['needs_review']


def test_conflict_retains_both_sides_and_missing_scope_without_winner():
    _, _, _, _, prepared, first = fixture()
    result = selection.decode_review(reply(
        {'verdict': 'conflicting', 'unit_ids': ['C1:rule', 'C1:deferred']}), prepared, first)
    assert result['status'] == 'conflicting'
    assert 'unresolved conflict for P1' in result['text']
    assert 'do not establish P2' in result['text']
    assert result['parts'][0]['unit_ids'] == ['C1:rule', 'C1:exception', 'C1:deferred']
    # Two IDs meet the structural minimum; incompatibility needs semantic review.
    assert result['needs_review']


def test_original_question_and_complete_claims_stay_bound_and_source_is_untrusted():
    question, evidence, _, plan, prepared, _ = fixture()
    payload = json.loads(selection.messages(prepared)[1]['content'])
    assert payload['question'] == question and payload['request'] == question
    assert payload['parts'] == plan['parts']
    assert 'untrusted data' in selection.messages(prepared)[0]['content']
    assert prepared['claims'][0]['sources'] == evidence[0]['sources']
    assert 'not in this prompt' in payload['support_visibility']
    evidence[0]['sources'][0]['content'] = 'Changed later.'
    assert prepared['claims'][0]['sources'][0]['content'] != 'Changed later.'


@pytest.mark.parametrize('raw', [
    '{}', '{"P1":[]}', '{"P1":[],"P2":[],"P3":[]}',
    '{"P1":[],"P1":[],"P2":[]}', '{"P1":{},"P2":[]}',
    '{"P1":["C1:absent"],"P2":[]}', '{"P1":[true],"P2":[]}',
    '{"P1":["C1:rule","C1:rule"],"P2":[]}', '[]', None])
def test_incomplete_or_malformed_proposal_is_rejected(raw):
    _, _, _, _, prepared, _ = fixture()
    with pytest.raises(ValueError):
        selection.decode_selection(raw, prepared)


@pytest.mark.parametrize('value', [
    {'verdict': 'supported', 'unit_ids': []},
    {'verdict': 'conflicting', 'unit_ids': ['C1:rule']},
    {'verdict': 'unknown', 'unit_ids': ['C1:absent']},
    {'verdict': 'unknown', 'unit_ids': ['C1:rule', 'C1:rule']},
    {'verdict': 'approved', 'unit_ids': ['C1:rule']},
    {'verdict': True, 'unit_ids': []},
    {'verdict': 'unknown', 'unit_ids': [], 'answer': 'invented'}, None])
def test_review_verdict_evidence_and_fields_are_strict(value):
    _, _, _, _, prepared, first = fixture()
    with pytest.raises(ValueError):
        selection.decode_review(json.dumps({'P1': value, 'P2': {'verdict': 'unknown', 'unit_ids': []}}),
                                prepared, first)


def test_replay_rejects_changed_source_plan_source_question_or_result():
    q, evidence, plans, plan, prepared, first = fixture()
    raw = reply()
    result = selection.decode_review(raw, prepared, first)
    assert selection.validate_result(q, evidence, plan, first['raw'], raw, result,
                                     unit_plans=plans) == result
    bad_evidence = deepcopy(evidence)
    bad_evidence[0]['sources'][0]['content'] = 'New support.'
    bad_plans = deepcopy(plans)
    bad_plans['claim:policy']['units'][0]['required_unit_ids'] = []
    bad_plan = parts.prepare(q, ['Who owns it?', 'When does it expire?'], origin='reviewer_supplied')
    for ev, pl, pp in [(bad_evidence, plans, plan), (evidence, bad_plans, plan),
                       (evidence, plans, bad_plan)]:
        with pytest.raises(ValueError):
            selection.validate_result(q, ev, pp, first['raw'], raw, result, unit_plans=pl)
    for field, value in [('text', 'Invented'), ('needs_review', False), ('coverage', 'verified'),
                         ('selection_input_sha256', '0' * 64), ('plan_sha256', '0' * 64)]:
        with pytest.raises(ValueError):
            selection.validate_result(q, evidence, plan, first['raw'], raw,
                                       {**result, field: value}, unit_plans=plans)
    with pytest.raises(ValueError):
        selection.prepare('Another question?', evidence, plan, unit_plans=plans)
    tampered = deepcopy(prepared)
    tampered['catalog']['units'][0]['text'] = 'Edited.'
    with pytest.raises(ValueError):
        selection.messages(tampered)
    tampered_first = {**first, 'context_unit_ids': []}
    with pytest.raises(ValueError):
        selection.review_schema(prepared, tampered_first)


def test_runtime_accepts_fixed_part_schemas_without_model_calls():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    _, _, _, _, prepared, first = fixture()
    for schema in [selection.response_schema(prepared), selection.review_schema(prepared, first)]:
        assert schema['required'] == ['P1', 'P2']
        assert _structured_response_format(object.__new__(SplitModel),
                                            {'type': 'json_object', 'schema': schema})


def test_dependency_and_visible_display_caps_fail_without_truncation():
    question = 'Rule?'
    plan = parts.prepare(question, ['Rule?'], origin='fixture_supplied')
    text = '\n'.join(f'Rule {i}.' for i in range(17))
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:rules', 'content': text,
                 'sources': [{'citation_id': 'source:rules', 'content': text}]}]
    source_plan = []
    cursor = 0
    for i, line in enumerate(text.splitlines()):
        source_plan.append({'id': f'r{i}', 'ranges': [[cursor, cursor + len(line)]],
                            'required_unit_ids': [f'r{i + 1}'] if i < 16 else []})
        cursor += len(line) + 1
    prepared = selection.prepare(question, evidence, plan, unit_plans={
        'claim:rules': {'source_sha256': units.text_hash(text), 'units': source_plan}})
    with pytest.raises(ValueError, match='closed unit limit'):
        selection.decode_selection('{"P1":["C1:r0"]}', prepared)
    evidence[0]['content'] = 'x' * 8000
    prepared = selection.prepare(question, evidence, plan)
    first = selection.decode_selection('{"P1":[]}', prepared)
    with pytest.raises(ValueError, match='visible answer limit'):
        selection.decode_review('{"P1":{"verdict":"unknown","unit_ids":["C1:whole"]}}',
                                prepared, first)
