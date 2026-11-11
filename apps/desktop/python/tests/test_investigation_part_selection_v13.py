from copy import deepcopy
import json
import pytest
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import reviewed_source_unit_plan as units

from potluck_local.services import investigation_part_selection_v13 as contract


def selection(prepared, assignments=None):
    assignments=assignments or {}
    raw=json.dumps({u['id']:assignments.get(u['id'],[]) for u in prepared['catalog']['units']})
    return contract.decode_selection(raw,prepared)


def setup():
    q='Which rule and deadline?'; text='Approval is required.\nEmergency work is exempt.\nNo approval is required.'
    lines=text.splitlines();offset=0; entries=[]
    for name,line in zip(('rule','exception','other'),lines):
        entries.append({'id':name,'ranges':[[offset,offset+len(line)]], 'required_unit_ids':['exception'] if name=='rule' else []});offset+=len(line)+1
    ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a','content':text}]}]
    plans={'claim:a':{'source_sha256':units.text_hash(text),'units':entries}}
    plan=parts.prepare(q,['Which rule?','Which deadline?'],origin='fixture_supplied')
    prepared=contract.prepare(q,ev,plan,unit_plans=plans); first=selection(prepared,{'C1:rule':['P1']})
    return q,ev,plans,plan,prepared,first


def item(answer=None,alternative=None,context=None,check='Source supports rule.'):
    return {'check':check,'answer':answer or [],'alternative':alternative or [],'context':context or []}


def wire(p1=None,p2=None,context=None):
    return json.dumps({'parts':{'P1':p1 or item(),'P2':p2 or item()},'context':context or []})


def test_supported_unknown_context_closure_and_replay():
    q,ev,plans,plan,p,first=setup();raw=wire(item(answer=['C1:rule'],check='INJECTED SECRET'),item(context=['C1:rule']),['C1:other'])
    r=contract.decode_review(raw,p,first)
    assert [row['verdict'] for row in r['parts']]==['supported','unknown'] and r['status']=='unknown'
    assert 'Emergency work is exempt.' in r['text'] and 'INJECTED' not in r['text']
    assert r['request_context']['visible_unit_ids']==['C1:other']
    assert contract.validate_result(q,ev,plan,first['raw'],raw,r,unit_plans=plans)==r
    bad=deepcopy(r);bad['parts'][0]['evidence_check']='forged'
    with pytest.raises(ValueError):contract.validate_result(q,ev,plan,first['raw'],raw,bad,unit_plans=plans)


def test_conflict_and_alternative_only_normalization():
    *_,p,first=setup()
    conflict=contract.decode_review(wire(item(answer=['C1:rule'],alternative=['C1:other'])),p,first)
    assert conflict['parts'][0]['verdict']=='conflicting' and 'no authoritative winner' in conflict['text'].lower()
    single=contract.decode_review(wire(item(alternative=['C1:other'])),p,first)
    assert single['parts'][0]['verdict']=='supported' and single['parts'][0]['answer_unit_ids']==['C1:other']


@pytest.mark.parametrize('bad', [
    {}, {'check':'x','answer':[],'alternative':[]},
    {'check':'x','answer':[],'alternative':[],'context':[],'extra':1},
    item(answer=['C1:rule','C1:rule']), item(answer=['C1:rule'],context=['C1:rule']),
    item(answer=['C1:foreign']), item(answer=[True]), item(check='x'*81), item(check='x\n')])
def test_invalid_envelopes_or_groups_fail_closed(bad):
    *_,p,first=setup()
    with pytest.raises(ValueError):contract.decode_review(json.dumps({'parts':{'P1':bad,'P2':item()},'context':[]}),p,first)


def test_duplicates_first_and_replay_binding():
    q,ev,plans,plan,p,first=setup();other=selection(p,{'C1:other':['P1']});raw=wire(item(answer=['C1:other']))
    with pytest.raises(ValueError):contract.decode_review('{"parts":{"P1":{"check":"x","answer":[],"answer":[],"alternative":[],"context":[]},"P2":{}},"context":[]}',p,first)
    r=contract.decode_review(raw,p,first)
    with pytest.raises(ValueError):contract.validate_result(q,ev,plan,other['raw'],raw,r,unit_plans=plans)
    old=deepcopy(p);old['version']='fixed-part-selection-v12'
    with pytest.raises(ValueError):contract.review_messages(old,first)
    assert list(contract.review_schema(p,first)['properties'])==['parts','context']


def test_global_union_bounds_and_inventory_are_independent():
    q='What applies?';lines=[f'Rule {i}.' for i in range(72)];text='\n'.join(lines);offset=0;entries=[]
    for i,line in enumerate(lines):
        entries.append({'id':f'u{i:02}','ranges':[[offset,offset+len(line)]],'required_unit_ids':[]});offset+=len(line)+1
    ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a','content':text}]}]
    plan=parts.prepare(q,[q],origin='fixture_supplied');plans={'claim:a':{'source_sha256':units.text_hash(text),'units':entries}}
    p=contract.prepare(q,ev,plan,unit_plans=plans);first=selection(p,{f'C1:u{i:02}':['P1'] for i in range(72)})
    assert len(first['inventory'])==72
    raw=json.dumps({'parts':{'P1':item(answer=['C1:u71'])},'context':[]})
    assert contract.decode_review(raw,p,first)['unit_ids']==['C1:u71']
    overflow=json.dumps({'parts':{'P1':item(answer=[f'C1:u{i:02}' for i in range(9)])},'context':[f'C1:u{i:02}' for i in range(9,17)]})
    with pytest.raises(ValueError):contract.decode_review(overflow,p,first)


@pytest.mark.parametrize('check',[None,True,7,'','   '])
def test_check_is_strict_metadata(check):
    *_,p,first=setup()
    with pytest.raises(ValueError):contract.decode_review(wire(item(check=check)),p,first)


def test_check_cannot_supply_or_repair_an_answer():
    *_,p,first=setup()
    raw=wire(item(check='Two values conflict!'),item(check='The deadline is ten days.'))
    result=contract.decode_review(raw,p,first)
    assert [r['verdict'] for r in result['parts']]==['unknown','unknown']
    assert not result['unit_ids'] and 'ten days' not in result['text']


def test_each_part_has_independent_conflict_groups_and_context_is_not_an_answer():
    *_,p,first=setup()
    raw=wire(item(answer=['C1:rule'],alternative=['C1:other']),item(context=['C1:rule','C1:other']))
    result=contract.decode_review(raw,p,first)
    assert [r['verdict'] for r in result['parts']]==['conflicting','unknown']
    assert result['parts'][0]['answer_unit_ids']==['C1:rule']
    assert result['parts'][0]['alternative_unit_ids']==['C1:other']
    assert result['parts'][1]['answer_unit_ids']==[] and result['parts'][1]['alternative_unit_ids']==[]


@pytest.mark.parametrize('field',['answer','alternative','context'])
def test_role_values_require_actual_lists(field):
    *_,p,first=setup();row=item();row[field]='C1:rule'
    with pytest.raises(ValueError):contract.decode_review(wire(row),p,first)
