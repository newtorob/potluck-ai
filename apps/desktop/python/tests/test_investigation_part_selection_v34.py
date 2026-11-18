"""V34 literal unresolved requests; structural checks are not semantic proof."""
import json
from copy import deepcopy

import pytest

from potluck_local.services import investigation_part_selection_v33 as v33
from potluck_local.services import investigation_part_selection_v34 as v34
from potluck_local.services import investigation_request_parts as parts
from test_investigation_single_review_v28 import prepared as fixture_source


def prepared(question='Which rule applies, and what is the exact deadline?'):
    _, evidence, plans, _, _ = fixture_source()
    plan = parts.prepare(question, [question], origin='fixture_supplied', request=question)
    value = v34.prepare(question, evidence, plan, request=question, unit_plans=plans)
    return question, evidence, plans, plan, value, v34.authored_inventory(value)


def wire(verdict='supported', evidence=None, unresolved=None, background=None, safeguards=None):
    return json.dumps({'parts': {'P1': {'verdict': verdict, 'evidence': evidence if evidence is not None else ['C1:rule'], 'background': background or [], 'unresolved': unresolved or []}}, 'safeguards': safeguards or []}, ensure_ascii=False)


def test_schema_and_full_supported_no_false_unknown():
    _, _, _, _, value, inventory = prepared()
    schema = v34.review_schema(value, inventory)['properties']['parts']['properties']['P1']
    assert 'unresolved' in schema['required']
    assert schema['properties']['unresolved']['maxItems'] == 6
    result = v34.decode_review(wire(), value, inventory)
    assert result['status'] == 'supported'
    assert result['parts'][0]['unresolved_requests'] == []
    assert result['parts'][0]['unresolved_spans'] == []
    assert result['unresolved_requests'] == []
    assert 'Approval is required.' in result['text']
    assert 'Emergency work is exempt.' in result['text']


def test_mixed_request_preserves_v33_passages_and_scope():
    question, evidence, plans, plan, value, inventory = prepared()
    fragment = 'what is the exact deadline'
    result = v34.decode_review(wire(unresolved=[fragment], background=['C1:other']), value, inventory)
    start = question.index(fragment)
    assert result['status'] == 'unknown'
    assert result['parts'][0]['unresolved_requests'] == [fragment]
    assert result['parts'][0]['unresolved_spans'] == [{'text': fragment, 'start': start, 'end': start + len(fragment)}]
    assert result['unresolved_requests'] == [{'part_id': 'P1', 'text': fragment, 'start': start, 'end': start + len(fragment)}]
    assert fragment in result['text'] and 'selected reviewed statements' in result['text'].lower()
    assert 'whole document' not in result['text'].lower()
    older = v33.prepare(question, evidence, plan, request=question, unit_plans=plans)
    old_inventory = v33.authored_inventory(older)
    old_raw = json.loads(wire(background=['C1:other']))
    del old_raw['parts']['P1']['unresolved']
    old = v33.decode_review(json.dumps(old_raw), older, old_inventory)
    for key in ('unit_ids', 'visible_unit_ids', 'background_only_unit_ids', 'source_text'):
        assert result[key] == old[key]
    for key in ('evidence_unit_ids', 'context_unit_ids', 'unit_ids', 'dependency_only_unit_ids', 'passages'):
        assert result['parts'][0][key] == old['parts'][0][key]
    assert 'Emergency work is exempt.' in result['text']
    assert result['parts'][0]['passages'][0]['citation_id'] == 'claim:a'


def test_unknown_keeps_scope_anchor_and_rejects_unanchored_final():
    _, _, _, _, value, inventory = prepared()
    raw = wire('unknown', ['C1:rule'], ['what is the exact deadline'])
    result = v34.decode_review(raw, value, inventory)
    assert result['status'] == 'unknown' and 'Approval is required.' in result['text']
    assert result['parts'][0]['passages']
    with pytest.raises(ValueError):
        v34.decode_review(wire('unknown', [], ['what is the exact deadline']), value, inventory)


def test_unicode_offsets_are_python_character_indices():
    question = 'Why is café allowed, and when is the deadline 🕒?'
    _, _, _, _, value, inventory = prepared(question)
    fragment = 'when is the deadline 🕒'
    result = v34.decode_review(wire(unresolved=[fragment]), value, inventory)
    span = result['parts'][0]['unresolved_spans'][0]
    assert question[span['start']:span['end']] == fragment
    assert span['start'] == question.index(fragment)


@pytest.mark.parametrize('unresolved', [
    ['what is the exact deadline', 'what is the exact deadline'],
    ['what is the exact deadline', 'the exact deadline'],
    ['outside the question'], [''], ['   '], [None], ['deadline'] * 7,
])
def test_rejects_invalid_duplicate_overlapping_or_unbound_requests(unresolved):
    _, _, _, _, value, inventory = prepared()
    with pytest.raises(ValueError):
        v34.decode_review(wire(unresolved=unresolved), value, inventory)


def test_missing_field_and_ambiguous_repeated_fragment_rejected():
    _, _, _, _, value, inventory = prepared()
    raw = json.loads(wire()); del raw['parts']['P1']['unresolved']
    with pytest.raises(ValueError): v34.decode_review(json.dumps(raw), value, inventory)
    _, _, _, _, repeated, first = prepared('When is approval due and when is approval due?')
    with pytest.raises(ValueError): v34.decode_review(wire(unresolved=['approval due']), repeated, first)
    _, _, _, _, repeated, first = prepared('What does aaa mean?')
    with pytest.raises(ValueError): v34.decode_review(wire(unresolved=['aa']), repeated, first)


def test_proposal_and_necessity_bind_question_source_and_wire():
    question, evidence, plans, plan, value, inventory = prepared()
    raw = wire(unresolved=['what is the exact deadline'])
    assert v34.decode_proposal(raw, value, inventory)['parts'][0]['unresolved_requests']
    assert v34.necessity_messages(value, inventory, raw)
    assert isinstance(v34.review_focus(value, inventory, raw), list)
    result = v34.decode_review(raw, value, inventory)
    assert v34.validate_result(question, evidence, plan, raw, result, request=question, unit_plans=plans) == result
    altered = deepcopy(evidence); altered[0]['content'] += ' changed'
    with pytest.raises(ValueError): v34.validate_result(question, altered, plan, raw, result, request=question, unit_plans=plans)
    with pytest.raises(ValueError): v34.validate_result(question + ' changed', evidence, plan, raw, result, request=question, unit_plans=plans)


def test_unknown_final_cannot_hide_which_request_is_unresolved():
    _, _, _, _, value, inventory = prepared()
    raw = wire('unknown', ['C1:rule'])
    assert v34.decode_proposal(raw, value, inventory)['status'] == 'unknown'
    with pytest.raises(ValueError, match='name an unresolved'):
        v34.decode_review(raw, value, inventory)
