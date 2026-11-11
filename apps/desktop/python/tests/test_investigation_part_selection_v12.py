from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import pytest
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import reviewed_source_unit_plan as units

from potluck_local.services import investigation_part_selection_v12 as contract


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


def envelope(unitmap=None,check='Requested rule; source states approval.'):
    return {'check':check,'units':unitmap or {}}



def wire(matrix=None, context=None, checks=None):
    return json.dumps({'checks': checks or {'P1':'Rule stated.','P2':'Deadline absent.'}, 'units':matrix or {}, 'context':context or []})


def test_matrix_status_closure_context_and_replay():
    q,ev,plans,plan,p,first=setup()
    raw=wire({'C1:rule':['a','c'], 'C1:other':['omit','omit']})
    unused=contract.decode_review(raw,p,first)
    assert unused['unit_ids']==['C1:rule','C1:exception'] and unused['review_raw']==raw
    assert 'No approval is required.' not in unused['text']
    raw=wire({'C1:rule':['a','c']},['C1:other'])
    result=contract.decode_review(raw,p,first)
    assert result['parts'][0]['verdict']=='supported' and result['parts'][1]['verdict']=='unknown'
    assert 'Emergency work is exempt.' in result['text'] and result['request_context']['visible_unit_ids']==['C1:other']
    assert contract.validate_result(q,ev,plan,first['raw'],raw,result,unit_plans=plans)==result
    tampered=deepcopy(result);tampered['request_context']['visible_unit_ids']=[]
    with pytest.raises(ValueError):contract.validate_result(q,ev,plan,first['raw'],raw,tampered,unit_plans=plans)


def test_b_only_is_supported_and_checks_are_not_evidence():
    *_,p,first=setup();raw=wire({'C1:rule':['b','omit']},checks={'P1':'INJECTED SECRET','P2':'Unknown.'})
    r=contract.decode_review(raw,p,first);assert r['parts'][0]['verdict']=='supported' and r['parts'][1]['verdict']=='unknown'
    assert r['parts'][0]['answer_unit_ids']==['C1:rule'] and 'INJECTED' not in r['text']


@pytest.mark.parametrize('matrix',[{'C1:rule':['a']},{'C1:rule':['a','omit','c']},{'C1:rule':['a',True]}, {'C1:foreign':['a','omit']}, {'C1:rule':['z','omit']}, {'C1:rule':'a'}])
def test_bad_matrix_rejected(matrix):
    *_,p,first=setup()
    with pytest.raises(ValueError):contract.decode_review(wire(matrix),p,first)


def test_duplicate_keys_and_old_version_reject():
    *_,p,first=setup()
    with pytest.raises(ValueError):contract.decode_review('{"checks":{"P1":"x","P2":"x"},"units":{"C1:rule":["a","omit"],"C1:rule":["b","omit"]},"context":[]}',p,first)
    old=deepcopy(p);old['version']='fixed-part-selection-v11'
    with pytest.raises(ValueError):contract.review_messages(old,first)
    assert list(contract.review_schema(p,first)['properties'])==['checks','units','context']


def test_independent_final_override_and_first_binding():
    q,ev,plans,plan,p,first=setup();other=selection(p,{'C1:other':['P1']})
    raw=wire({'C1:other':['a','omit']})
    result=contract.decode_review(raw,p,first)
    assert result['unit_ids']==['C1:other']
    with pytest.raises(ValueError):contract.validate_result(q,ev,plan,other['raw'],raw,result,unit_plans=plans)
    assert contract.validate_result(q,ev,plan,first['raw'],raw,result,unit_plans=plans)==result


@pytest.mark.parametrize('context',[['C1:rule','C1:rule'],['C1:foreign'],[True],'C1:rule'])
def test_invalid_global_context_rejects(context):
    *_,p,first=setup()
    with pytest.raises(ValueError):contract.decode_review(wire(context=context),p,first)


def test_distinct_alternatives_and_scoped_context():
    *_,p,first=setup();raw=wire({'C1:rule':['a','omit'],'C1:other':['b','omit']},['C1:exception'])
    r=contract.decode_review(raw,p,first)
    assert r['parts'][0]['verdict']=='conflicting' and r['parts'][1]['verdict']=='unknown'
    assert r['status']=='conflicting' and 'no authoritative winner' in r['text'].lower()


def test_inventory72_is_metadata_and_union_limit_still_applies():
    q='What applies?';lines=[f'Rule {i}.' for i in range(72)];text='\n'.join(lines);offset=0;entries=[]
    for i,line in enumerate(lines):
        entries.append({'id':f'u{i:02}','ranges':[[offset,offset+len(line)]],'required_unit_ids':[]});offset+=len(line)+1
    ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a','content':text}]}]
    plan=parts.prepare(q,[q],origin='fixture_supplied');plans={'claim:a':{'source_sha256':units.text_hash(text),'units':entries}}
    p=contract.prepare(q,ev,plan,unit_plans=plans);first=selection(p,{f'C1:u{i:02}':['P1'] for i in range(72)})
    assert len(first['inventory'])==72
    raw=json.dumps({'checks':{'P1':'Rule stated.'},'units':{'C1:u71':['a']},'context':[]})
    assert contract.decode_review(raw,p,first)['unit_ids']==['C1:u71']
    overflow=json.dumps({'checks':{'P1':'Rule stated.'},'units':{f'C1:u{i:02}':['a'] for i in range(9)},'context':[f'C1:u{i:02}' for i in range(9,17)]})
    with pytest.raises(ValueError):contract.decode_review(overflow,p,first)


@pytest.mark.parametrize('checks', [{}, {'P1':'ok'}, {'P1':'ok','P2':'ok','P3':'extra'}, {'P1':'','P2':'ok'}, {'P1':'   ','P2':'ok'}, {'P1':'x'*81,'P2':'ok'}, {'P1':False,'P2':'ok'}, {'P1':'injected\\n','P2':'ok'}])
def test_checks_strict_and_never_evidence(checks):
    *_,p,first=setup()
    if checks.get('P1') == 'injected\\n':
        checks['P1']='injected\n'
    with pytest.raises(ValueError):
        contract.decode_review(json.dumps({'checks':checks,'units':{},'context':[]}),p,first)


def test_all_unused_rows_preserve_raw_without_creating_support_or_evidence():
    *_,p,first=setup()
    raw=wire({'C1:rule':['omit','omit'],'C1:other':['omit','omit']})
    result=contract.decode_review(raw,p,first)
    assert result['status']=='unknown' and result['unit_ids']==[]
    assert result['review_raw']==raw and all(row['unit_ids']==[] for row in result['parts'])
    assert 'Approval is required.' not in result['text']


def test_six_part_role_positions_are_exact_and_context_is_not_support():
    q='What applies?';text='Source rule.'
    ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a','content':text}]}]
    plan=parts.prepare(q,[f'Question {i}?' for i in range(6)],origin='fixture_supplied')
    p=contract.prepare(q,ev,plan);first=selection(p)
    schema=contract.review_schema(p,first)['properties']['units']['properties']['C1:whole']
    assert schema['minItems']==6 and schema['maxItems']==6
    raw=json.dumps({'checks':{f'P{i}':'Bounded check.' for i in range(1,7)},'units':{'C1:whole':['a','omit','c','b','omit','a']},'context':[]})
    result=contract.decode_review(raw,p,first)
    assert [r['verdict'] for r in result['parts']]==['supported','unknown','unknown','supported','unknown','supported']
    assert result['parts'][2]['context_unit_ids']==['C1:whole']
    assert result['parts'][3]['answer_unit_ids']==['C1:whole']
