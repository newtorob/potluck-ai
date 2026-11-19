"""Prompt-version isolation and exact unresolved evidence replay."""
import json
import pytest

from potluck_local.services import investigation_part_selection_v34 as old
from potluck_local.services import investigation_part_selection_v35 as current
from potluck_local.services import investigation_request_parts as parts
from test_investigation_single_review_v28 import prepared as fixture_source
from test_investigation_part_selection_v34 import wire


def prepared():
    question = 'Which rule applies, and what is the exact deadline?'
    _, evidence, plans, _, _ = fixture_source()
    plan = parts.prepare(question, [question], origin='fixture_supplied', request=question)
    value = current.prepare(question, evidence, plan, request=question, unit_plans=plans)
    return question, evidence, plans, plan, value, current.authored_inventory(value)


def test_coverage_audit_is_independent_version_with_unchanged_wire_and_sources():
    question, evidence, plans, plan, value, first = prepared()
    previous = old.prepare(question, evidence, plan, request=question, unit_plans=plans)
    previous_first = old.authored_inventory(previous)
    raw = wire()
    old_messages = old.necessity_messages(previous, previous_first, raw)
    messages = current.necessity_messages(value, first, raw)
    assert old_messages[0]['content'].startswith('Edit the proposed source selection')
    assert messages[0]['content'].startswith('Independently audit coverage FIRST.')
    assert len(messages[0]['content']) < len(old_messages[0]['content'])
    assert 'ONLY AFTER the coverage audit' in messages[0]['content']
    assert json.loads(messages[1]['content'])['claims'] == json.loads(old_messages[1]['content'])['claims']
    assert current.review_schema(value, first) == old.review_schema(previous, previous_first)
    assert current.review_messages(value, first) == old.review_messages(previous, previous_first)
    assert current.REVIEW_INSTRUCTION == old.REVIEW_INSTRUCTION
    assert current.review_focus(value, first, raw) == old.review_focus(previous, previous_first, raw)


def test_exact_clauses_conditions_and_version_bound_replay():
    question, evidence, plans, plan, value, first = prepared()
    raw = wire(unresolved=['what is the exact deadline'])
    result = current.decode_review(raw, value, first)
    assert result['status'] == 'unknown'
    assert result['version'] == 'fixed-part-selection-v35'
    assert 'Approval is required.' in result['text']
    assert 'Emergency work is exempt.' in result['text']
    assert result['parts'][0]['passages'][0]['citation_id'] == 'claim:a'
    assert current.validate_result(question, evidence, plan, raw, result, request=question, unit_plans=plans) == result
    with pytest.raises(ValueError):
        old.validate_result(question, evidence, plan, raw, result, request=question, unit_plans=plans)
