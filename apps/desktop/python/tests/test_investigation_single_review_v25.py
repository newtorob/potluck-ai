import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
import pytest

from potluck_local.services import investigation_part_selection_v25 as v
from potluck_local.services import investigation_fixed_part_answers_v25 as a
from test_investigation_part_selection_v22 import setup, wire
from potluck_local.services import investigation_reviewed_evidence

def prepared():
    q, ev, plans, plan, _, _ = setup()
    return q, ev, plans, plan, v.prepare(q, ev, plan, unit_plans=plans)

def test_review_replay_and_authored_inventory():
    q, ev, plans, plan, p = prepared()
    first = v.authored_inventory(p)
    assert all(not labels for labels in first['inventory'].values())
    assert 'not-model-reply' in first['origin']
    messages = v.review_messages(p, first)
    assert 'inventory' not in json.dumps(messages).lower()
    payload=json.loads(messages[1]['content'])
    assert len([u for claim in payload['claims'] for u in claim['units']]) == len(p['catalog']['units'])
    schema=v.review_schema(p,first)['properties']['parts']['properties']['P1']
    assert list(schema['properties']) == ['evidence','context','check','verdict']
    assert schema['required'] == ['evidence','context','check','verdict']
    raw = wire('conflicting', ['C1:rule'])
    result = v.decode_review(raw, p, first)
    assert 'selection_raw' not in result and result['review_raw'] == raw
    assert 'C1:exception' in result['parts'][0]['unit_ids']
    assert 'no authoritative winner' in result['text']
    assert v.validate_result(q, ev, plan, raw, result, unit_plans=plans) == result
    forged = deepcopy(first); forged['inventory']['C1:rule'] = ['P1:evidence']
    with pytest.raises(ValueError): v.review_messages(p, forged)

def test_one_reply_envelope_rejects_fake_selection():
    q, ev, plans, plan, p = prepared()
    snippets, _ = investigation_reviewed_evidence.snippets(ev)
    op = {'id': 'q', 'request': q}
    raw = wire('unknown', ['C1:rule'])
    brief, trace = a.make_brief(q, op, ev, snippets, json.dumps({'review_raw': raw}), question_part_plan=plan, unit_plans=plans)
    assert 'Scope basis:' in brief['answers'][0]['text']
    assert set(json.loads(trace['raw'])) == {'review_raw'}
    assert 'selection' not in trace
    for invalid in (json.dumps({'selection_raw': '{}', 'review_raw': raw}), '{"review_raw":"a","review_raw":"b"}'):
        with pytest.raises(ValueError): a.make_brief(q, op, ev, snippets, invalid, question_part_plan=plan, unit_plans=plans)

def test_injected_contract_rejected():
    q, ev, plans, plan, p = prepared()
    snippets, _ = investigation_reviewed_evidence.snippets(ev)
    from potluck_local.services import investigation_part_selection_v22 as old
    with pytest.raises(ValueError, match='Invalid V25 contract'):
        a.make_brief(q, {'id':'q','request':q}, ev, snippets, json.dumps({'review_raw':wire('unknown',[])}), question_part_plan=plan, unit_plans=plans, _contract=old)
    with pytest.raises(ValueError, match='Invalid V25 contract'):
        a.validate_brief({}, snippets, _contract=old)

def test_full_catalog_and_all_parts_are_offered_without_inventory_or_prior_reply():
    q,ev,plans,plan,p=prepared();first=v.authored_inventory(p)
    messages=v.review_messages(p,first);payload=json.loads(messages[1]['content'])
    assert payload['question']==q
    assert {part['id'] for part in payload['parts']}=={part['id'] for part in plan['parts']}
    assert {unit['id'] for claim in payload['claims'] for unit in claim['units']}=={unit['id'] for unit in p['catalog']['units']}
    text=json.dumps(messages).lower()
    assert all(term not in text for term in ('inventory','selection_raw','prior reply','model reply'))
    assert 'before deciding whether a requested detail is absent' in messages[0]['content']
