"""Instruction contract checks; these do not establish model correctness."""
import json
from test_focused_review_v32 import setup
from test_investigation_single_review_v28 import wire
from potluck_local.services import investigation_part_selection_v28 as baseline
from potluck_local.services import investigation_part_selection_v32 as v32
from potluck_local.services import investigation_part_selection_v33 as v33


def test_v32_wire_unchanged_and_v33_adds_observation_scope_to_both_calls():
    q, evidence, plans, plan, old, old_inventory = setup()
    prior = baseline.prepare(q, evidence, plan, unit_plans=plans)
    prior_inventory = baseline.authored_inventory(prior)
    raw = wire('supported', ['C1:rule'])
    assert v32.review_messages(old, old_inventory) == baseline.review_messages(prior, prior_inventory)
    assert v32.necessity_messages(old, old_inventory, raw) == baseline.necessity_messages(prior, prior_inventory, raw)
    candidate = v33.prepare(q, evidence, plan, unit_plans=plans)
    inventory = v33.authored_inventory(candidate)
    assert v33.review_schema(candidate, inventory) == v32.review_schema(old, old_inventory)
    for messages in (v33.review_messages(candidate, inventory), v33.necessity_messages(candidate, inventory, raw)):
        system = messages[0]['content']
        assert 'exact subject, event, time and observation scope' in system
        assert 'actual total when telemetry is missing' in system
        assert 'complete explicit count or scoped negative can still support an answer' in system
        assert json.loads(messages[1]['content'])['contract'] == baseline.VERSION
    assert 'actual total when telemetry is missing' not in v32.REVIEW_INSTRUCTION
