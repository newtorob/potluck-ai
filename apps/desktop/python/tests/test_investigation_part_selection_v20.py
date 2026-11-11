from copy import deepcopy
import json
from pathlib import Path
import pytest
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import reviewed_source_unit_plan as units

from potluck_local.services import investigation_part_selection_v20 as v8


def selection(prepared, assignments=None):
    assignments=assignments or {}
    part_ids=[x['id'] for x in prepared['part_plan']['parts']]
    rows=[];context=[]
    for unit in prepared['catalog']['units']:
        labels=assignments.get(unit['id'],[]);row=[]
        for part in part_ids:
            evidence=part+':evidence' in labels;qualifier=part+':qualifier' in labels
            row.append('b' if evidence and qualifier else 'e' if evidence else 'q' if qualifier else 'n')
        rows.append(''.join(row)+('1' if 'request_context' in labels else '0'))
    raw=json.dumps({'inventory':rows})
    return v8.decode_selection(raw,prepared)


def setup():
    q='Which rule and deadline?'; text='Approval is required.\nEmergency work is exempt.\nNo approval is required.'
    lines=text.splitlines();offset=0; entries=[]
    for name,line in zip(('rule','exception','other'),lines):
        entries.append({'id':name,'ranges':[[offset,offset+len(line)]], 'required_unit_ids':['exception'] if name=='rule' else []});offset+=len(line)+1
    ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a','content':text}]}]
    plans={'claim:a':{'source_sha256':units.text_hash(text),'units':entries}}
    plan=parts.prepare(q,['Which rule?','Which deadline?'],origin='fixture_supplied')
    prepared=v8.prepare(q,ev,plan,unit_plans=plans); first=selection(prepared,{'C1:rule':['P1:evidence']})
    return q,ev,plans,plan,prepared,first




def wire(verdict='supported',answer=None,context=None):
    def row(v,a,c):return {'check':'Source states rule.','verdict':v,'evidence':a,'context':c}
    return json.dumps({'parts':{'P1':row(verdict,answer or [],context or []),'P2':row('unknown',[],[])},'context':[]})

def test_structurally_valid_relation_does_not_prove_truth_and_retains_deps():
    *_,p,first=setup()
    result=v8.decode_review(wire('conflicting',['C1:rule']),p,first)
    row=result['parts'][0]
    assert row['verdict']=='conflicting' and row['evidence_unit_ids']==['C1:rule'] and 'C1:exception' in row['unit_ids']
    assert 'no authoritative winner' in result['text']
    assert v8.validate_result(p['question'],[{'kind':'reviewed_claim','citation_id':c['citation_id'],'content':c['text'],'sources':c['sources']} for c in p['claims']],p['part_plan'],first['raw'],wire('conflicting',['C1:rule']),result,unit_plans={'claim:a':p['catalog']['plans']['C1']})==result

@pytest.mark.parametrize('verdict,answer',[('supported',[]),('conflicting',[]),('invalid',['C1:rule']),(True,['C1:rule'])])
def test_invalid_relation_evidence(verdict,answer):
    *_,p,first=setup()
    with pytest.raises(ValueError):v8.decode_review(wire(verdict,answer),p,first)

def test_unknown_context_and_old_alternative_wire_rejected():
    *_,p,first=setup()
    result=v8.decode_review(wire('unknown',context=['C1:other']),p,first)
    assert result['parts'][0]['verdict']=='unknown' and 'C1:other' in result['parts'][0]['context_unit_ids']
    raw=json.loads(wire('supported',['C1:rule']));raw['parts']['P1']['alternative']=[]
    with pytest.raises(ValueError):v8.decode_review(json.dumps(raw),p,first)

def test_typed_inventory_and_actual_runtime_admission():
    from potluck_local.services import runtime,split_native
    *_,p,first=setup()
    for schema in (v8.response_schema(p),v8.review_schema(p,first)):
        fmt={'type':'json_object','schema':schema}
        assert runtime._structured_response_format(object.__new__(split_native.SplitModel),fmt)==fmt
    with pytest.raises(ValueError):v8.decode_selection(json.dumps({'inventory':[['P1'],[],[]]}),p)

def test_raw_and_source_tampering_rejected():
    *_,p,first=setup();bad=deepcopy(first);bad['inventory']['C1:rule']=[]
    with pytest.raises(ValueError):v8.review_messages(p,bad)
    bad=deepcopy(p);bad['claims'][0]['text']+='changed'
    with pytest.raises(ValueError):v8.messages(bad)


def test_single_unit_explicit_comparison_supports_conflict_without_alternative_partition():
    question='Are the two stated defaults consistent?'
    text='The first default is seven days; the second is fourteen days. These defaults conflict.'
    evidence=[{'kind':'reviewed_claim','citation_id':'claim:comparison','content':text,'sources':[{'citation_id':'source:comparison','content':text}]}]
    plan=parts.prepare(question,[question],origin='fixture_supplied')
    prepared=v8.prepare(question,evidence,plan);uid=prepared['catalog']['units'][0]['id']
    first=v8.decode_selection(json.dumps({'inventory':['e0']}),prepared)
    raw=json.dumps({'parts':{'P1':{'check':'Different defaults conflict.','verdict':'conflicting','evidence':[uid],'context':[]}},'context':[]})
    result=v8.decode_review(raw,prepared,first)
    assert result['parts'][0]['verdict']=='conflicting' and text in result['text']
    assert result['parts'][0]['evidence_unit_ids']==[uid]


def test_unknown_can_include_scope_basis_without_asserting_semantic_absence():
    *_,p,first=setup()
    result=v8.decode_review(wire('unknown',['C1:rule']),p,first)
    row=result['parts'][0]
    assert row['verdict']=='unknown' and row['evidence_unit_ids']==['C1:rule']
    assert 'answer_unit_ids' not in row and 'Scope basis:' in result['text']
    assert 'Not established by the selected reviewed statements' in result['text']
    assert 'Approval is required.' in result['text'] and 'Source states rule.' not in result['text']
    # Structural acceptance is not an assertion that the requested fact is absent.


def test_old_answer_wire_and_rehashed_result_tamper_reject():
    *_,p,first=setup();raw=json.loads(wire('unknown',['C1:rule']))
    raw['parts']['P1']['answer']=raw['parts']['P1'].pop('evidence')
    with pytest.raises(ValueError):v8.decode_review(json.dumps(raw),p,first)
    good=wire('unknown',['C1:rule']);result=v8.decode_review(good,p,first)
    bad=deepcopy(result);bad['parts'][0]['evidence_unit_ids']=[]
    with pytest.raises(ValueError):v8.validate_result(p['question'],[{'kind':'reviewed_claim','citation_id':c['citation_id'],'content':c['text'],'sources':c['sources']} for c in p['claims']],p['part_plan'],first['raw'],good,bad,unit_plans={'claim:a':p['catalog']['plans']['C1']})


def test_fixed_cells_both_context_and_strict_old_wire_rejection():
    *_,prepared,first=setup()
    payload=json.loads(v8.messages(prepared)[1]['content'])
    assert payload['inventory_part_order']==['P1','P2']
    result=v8.decode_selection(json.dumps({'inventory':['bn1','nq0','nn0']}),prepared)
    assert result['inventory']['C1:rule']==['P1:evidence','P1:qualifier','request_context']
    assert result['inventory']['C1:exception']==['P2:qualifier']
    bad_values=[{'inventory':[['P1:evidence'],[],[]]}, {'inventory':[['evidence'],['none'],['none']],'context':[False]*3}, {'inventory':[['none','none']]*3,'context':[False,0,False]}, {'inventory':[['other','none']]*3,'context':[False]*3}, {'inventory':[['none','none']]*2,'context':[False]*3}]
    for value in bad_values:
        with pytest.raises(ValueError):v8.decode_selection(json.dumps(value),prepared)
    assert v8.review_messages(prepared,result)


def test_72_unit_inventory_not_limited_by_final_evidence_cap():
    q='Which rule?';lines=[f'Rule {i}.' for i in range(72)];text='\n'.join(lines);offset=0;entries=[]
    for i,line in enumerate(lines):
        entries.append({'id':f'u{i:02}','ranges':[[offset,offset+len(line)]],'required_unit_ids':[]});offset+=len(line)+1
    ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a','content':text}]}]
    plans={'claim:a':{'source_sha256':units.text_hash(text),'units':entries}};plan=parts.prepare(q,[q],origin='fixture_supplied')
    p=v8.prepare(q,ev,plan,unit_plans=plans);first=v8.decode_selection(json.dumps({'inventory':['e0']*72}),p)
    assert len(first['inventory'])==72
    raw=json.dumps({'parts':{'P1':{'check':'Rule stated.','verdict':'supported','evidence':['C1:u71'],'context':[]}},'context':[]})
    assert v8.decode_review(raw,p,first)['unit_ids']==['C1:u71']
    raw=json.dumps({'parts':{'P1':{'check':'Rule stated.','verdict':'supported','evidence':[f'C1:u{i:02}' for i in range(17)],'context':[]}},'context':[]})
    with pytest.raises(ValueError):v8.decode_review(raw,p,first)


def test_evidence_overlap_duplicate_and_bad_context_reject():
    *_,prepared,first=setup();raw=json.loads(wire('unknown',['C1:rule']))
    for bad in (['C1:rule','C1:rule'],[True],['C1:foreign']):
        candidate=deepcopy(raw);candidate['parts']['P1']['evidence']=bad
        with pytest.raises(ValueError):v8.decode_review(json.dumps(candidate),prepared,first)
    raw['parts']['P1']['context']=['C1:rule']
    with pytest.raises(ValueError):v8.decode_review(json.dumps(raw),prepared,first)


def test_check_length_boundaries_and_no_control_characters():
    *_,prepared,first=setup()
    raw=json.loads(wire('unknown',['C1:rule']))
    raw['parts']['P1']['check']='x'*40
    assert v8.decode_review(json.dumps(raw),prepared,first)['parts'][0]['evidence_check']=='x'*40
    for value in ('x'*41,'\n',''):
        raw['parts']['P1']['check']=value
        with pytest.raises(ValueError):v8.decode_review(json.dumps(raw),prepared,first)


