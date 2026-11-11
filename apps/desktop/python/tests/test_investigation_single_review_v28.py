import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
import pytest

from potluck_local.services import investigation_part_selection_v28 as v
from potluck_local.services import investigation_fixed_part_answers_v28 as a
from test_investigation_part_selection_v22 import setup, wire as prior_wire
from potluck_local.services import investigation_reviewed_evidence

def wire(verdict, ids):
    value=json.loads(prior_wire(verdict,ids))
    for part in value['parts'].values():
        part.pop('check');part['background']=part.pop('context')
    value['safeguards']=value.pop('context')
    return json.dumps(value)

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
    assert list(schema['properties']) == ['evidence','background','verdict']
    assert schema['required'] == ['evidence','background','verdict']
    raw = wire('conflicting', ['C1:rule'])
    result = v.decode_review(raw, p, first)
    assert 'selection_raw' not in result and result['review_raw'] == raw
    assert 'C1:exception' in result['parts'][0]['unit_ids']
    assert 'no authoritative winner' in result['text']
    assert v.validate_result(q, ev, plan, raw, result, unit_plans=plans) == result
    forged = deepcopy(first); forged['inventory']['C1:rule'] = ['P1:evidence']
    with pytest.raises(ValueError): v.review_messages(p, forged)

def test_two_actual_replies_and_necessity_input_bound():
    q,ev,plans,plan,p=prepared();first=v.authored_inventory(p)
    snippets,_=investigation_reviewed_evidence.snippets(ev)
    proposed=wire('supported',['C1:rule']);reviewed=wire('unknown',[])
    messages=v.necessity_messages(p,first,proposed)
    payload=json.loads(messages[1]['content'])
    assert payload['proposed_selection']==json.loads(proposed)
    assert payload['claims']==json.loads(v.review_messages(p,first)[1]['content'])['claims']
    assert 'strict deletion test' in messages[0]['content']
    brief,trace=a.make_brief(q,{'id':'q','request':q},ev,snippets,json.dumps({'selection_raw':proposed,'review_raw':reviewed}),question_part_plan=plan,unit_plans=plans)
    assert trace['proposal']['parts'][0]['verdict']=='supported' and trace['review']['status']=='unknown'
    assert brief['answers'][0]['status']=='unknown'
    for invalid in (json.dumps({'review_raw':reviewed}),json.dumps({'selection_raw':'{}','review_raw':reviewed})):
        with pytest.raises(ValueError): a.make_brief(q,{'id':'q','request':q},ev,snippets,invalid,question_part_plan=plan,unit_plans=plans)

def test_injected_contract_rejected():
    q, ev, plans, plan, p = prepared()
    snippets, _ = investigation_reviewed_evidence.snippets(ev)
    from potluck_local.services import investigation_part_selection_v22 as old
    with pytest.raises(ValueError, match='Invalid V28 contract'):
        a.make_brief(q, {'id':'q','request':q}, ev, snippets, json.dumps({'review_raw':wire('unknown',[])}), question_part_plan=plan, unit_plans=plans, _contract=old)
    with pytest.raises(ValueError, match='Invalid V28 contract'):
        a.validate_brief({}, snippets, _contract=old)

def test_full_catalog_and_all_parts_are_offered_without_inventory_or_prior_reply():
    q,ev,plans,plan,p=prepared();first=v.authored_inventory(p)
    messages=v.review_messages(p,first);payload=json.loads(messages[1]['content'])
    assert payload['question']==q
    assert {part['id'] for part in payload['parts']}=={part['id'] for part in plan['parts']}
    assert {unit['id'] for claim in payload['claims'] for unit in claim['units']}=={unit['id'] for unit in p['catalog']['units']}
    text=json.dumps(messages).lower()
    assert all(term not in text for term in ('inventory','selection_raw','prior reply','model reply'))
    assert 'before selecting source evidence or deciding a detail is absent' in messages[0]['content']
