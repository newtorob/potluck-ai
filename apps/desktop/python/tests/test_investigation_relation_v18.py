from copy import deepcopy
import json
import pytest
from test_local_server import server
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import reviewed_source_unit_plan as units

from potluck_local.services import investigation_part_selection_v18 as contract


def selection(prepared, assignments=None):
    assignments=assignments or {}
    raw=json.dumps({'inventory':[assignments.get(u['id'],[]) for u in prepared['catalog']['units']]})
    return contract.decode_selection(raw,prepared)


def setup():
    q='Which rule and deadline?'; text='Approval is required.\nEmergency work is exempt.\nNo approval is required.'
    lines=text.splitlines();offset=0; entries=[]
    for name,line in zip(('rule','exception','other'),lines):
        entries.append({'id':name,'ranges':[[offset,offset+len(line)]], 'required_unit_ids':['exception'] if name=='rule' else []});offset+=len(line)+1
    ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a','content':text}]}]
    plans={'claim:a':{'source_sha256':units.text_hash(text),'units':entries}}
    plan=parts.prepare(q,['Which rule?','Which deadline?'],origin='fixture_supplied')
    prepared=contract.prepare(q,ev,plan,unit_plans=plans); first=selection(prepared,{'C1:rule':['P1:answer']})
    return q,ev,plans,plan,prepared,first




def wire(verdict='supported',answer=None,context=None):
    def row(v,a,c):return {'check':'Source states rule.','verdict':v,'answer':a,'context':c}
    return json.dumps({'parts':{'P1':row(verdict,answer or [],context or []),'P2':row('unknown',[],[])},'context':[]})

def test_structurally_valid_relation_does_not_prove_truth_and_retains_deps():
    *_,p,first=setup()
    result=contract.decode_review(wire('conflicting',['C1:rule']),p,first)
    row=result['parts'][0]
    assert row['verdict']=='conflicting' and row['alternative_unit_ids']==[] and 'C1:exception' in row['unit_ids']
    assert 'no authoritative winner' in result['text']
    assert contract.validate_result(p['question'],[{'kind':'reviewed_claim','citation_id':c['citation_id'],'content':c['text'],'sources':c['sources']} for c in p['claims']],p['part_plan'],first['raw'],wire('conflicting',['C1:rule']),result,unit_plans={'claim:a':p['catalog']['plans']['C1']})==result

@pytest.mark.parametrize('verdict,answer',[('unknown',['C1:rule']),('supported',[]),('conflicting',[]),('invalid',['C1:rule']),(True,['C1:rule'])])
def test_invalid_relation_evidence(verdict,answer):
    *_,p,first=setup()
    with pytest.raises(ValueError):contract.decode_review(wire(verdict,answer),p,first)

def test_unknown_context_and_old_alternative_wire_rejected():
    *_,p,first=setup()
    result=contract.decode_review(wire('unknown',context=['C1:other']),p,first)
    assert result['parts'][0]['verdict']=='unknown' and 'C1:other' in result['parts'][0]['context_unit_ids']
    raw=json.loads(wire('supported',['C1:rule']));raw['parts']['P1']['alternative']=[]
    with pytest.raises(ValueError):contract.decode_review(json.dumps(raw),p,first)

def test_typed_inventory_has_distinct_answer_qualifier_names_without_bare_alias():
    *_,p,first=setup()
    labels=contract.response_schema(p)['properties']['inventory']['items']['items']['enum']
    assert labels==['P1:answer','P1:qualifier','P2:answer','P2:qualifier','request_context']
    typed=selection(p,{'C1:rule':['P1:qualifier']})
    assert typed['inventory']['C1:rule']==['P1:qualifier']
    # Review is allowed to correct a typed applicability role; it is metadata.
    result=contract.decode_review(wire('supported',['C1:rule']),p,typed)
    assert result['parts'][0]['answer_unit_ids']==['C1:rule']
    with pytest.raises(ValueError):contract.decode_selection(json.dumps({'inventory':[['P1'],[],[]]}),p)


def test_raw_and_source_tampering_rejected():
    *_,p,first=setup();bad=deepcopy(first);bad['inventory']['C1:rule']=[]
    with pytest.raises(ValueError):contract.review_messages(p,bad)
    bad=deepcopy(p);bad['claims'][0]['text']+='changed'
    with pytest.raises(ValueError):contract.messages(bad)


def test_single_unit_explicit_comparison_supports_conflict_without_alternative_partition():
    question='Are the two stated defaults consistent?'
    text='The first default is seven days; the second is fourteen days. These defaults conflict.'
    evidence=[{'kind':'reviewed_claim','citation_id':'claim:comparison','content':text,'sources':[{'citation_id':'source:comparison','content':text}]}]
    plan=parts.prepare(question,[question],origin='fixture_supplied')
    prepared=contract.prepare(question,evidence,plan);uid=prepared['catalog']['units'][0]['id']
    first=contract.decode_selection(json.dumps({'inventory':[['P1:answer']]}),prepared)
    raw=json.dumps({'parts':{'P1':{'check':'Different defaults conflict.','verdict':'conflicting','answer':[uid],'context':[]}},'context':[]})
    result=contract.decode_review(raw,prepared,first)
    assert result['parts'][0]['verdict']=='conflicting' and text in result['text']
    assert result['parts'][0]['alternative_unit_ids']==[]
