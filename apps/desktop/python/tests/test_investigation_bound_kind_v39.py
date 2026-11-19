import hashlib
import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_part_selection_v39 as v
from potluck_local.services import investigation_fixed_part_answers_v39 as adapter
from potluck_local.services.runtime import _structured_response_format
from potluck_local.services.split_native import SplitModel
from test_investigation_direct_source_answers_v38 import prepared as old_fixture, proposal, finding


def fixture():
    q,e,plans,plan,_,_=old_fixture()
    p=v.prepare(q,e,plan,request=q,unit_plans=plans)
    return q,e,plans,plan,p,v.authored_inventory(p)


def inputs():
    *_,p,first=fixture()
    c=proposal('Which rule applies, ','and what is the exact deadline?')
    raw=json.dumps({'F1':finding(), 'F2':finding('supplied_text_yes_no','no')})
    return p,first,c,raw


def test_exact_instruction_hashes_schema_and_guard():
    assert hashlib.sha256(v.DRAFT.encode()).hexdigest()=='b9bcb1fed9ffa327fc2f3d9c2da83624aa1e895934dd5a88cb7084f8a9f1b0c0'
    assert hashlib.sha256(v.AUDIT.encode()).hexdigest()=='ff3a33ada9a9e815736d31deb301843edf0f86f951643d0f23bc40b493aa5672'
    p,first,c,raw=inputs()
    for schema in (v.review_schema(p,first,c),v.necessity_schema(p,first,c,raw)):
        fmt={'type':'json_object','schema':schema}
        assert _structured_response_format(object.__new__(SplitModel),fmt)==fmt
    assert adapter.clause_receipt(p,first)['model_input_contract']=='fixed-part-selection-v36'
    assert adapter.review_receipt(p,first,c)['model_input_contract']=='fixed-part-selection-v38'
    assert adapter.necessity_receipt(p,first,c,raw)['model_input_contract']=='fixed-part-selection-v39'


def test_deferred_proposal_and_bound_final_preserve_conditions():
    p,first,c,raw=inputs();proposal_data=json.loads(raw)
    proposal_data['F1']['source_answer']='no'
    proposal_data['F2']['source_answer']='not_applicable'
    raw=json.dumps(proposal_data)
    proposed=v.decode_proposal(raw,p,first,c)
    assert proposed['ignored_nonselected_fields']['F1']=={'field':'source_answer','value':'no'}
    assert proposed['fixed_question_kinds']=={'F1':'substantive_rule_or_value','F2':'supplied_text_yes_no'}
    assert proposed['deferred_selected_issues'][0]['fact_id']=='F2'
    assert 'unverified' in proposed['proposal_validation']
    final=json.dumps({'F1':{'decision':'established','sources':['C1:rule']},'F2':{'decision':'no','sources':['C1:rule']}})
    result=v.decode_review(final,p,first,c,raw)
    assert result['status']=='supported' and result['acknowledgements'][0]['answer']=='No'
    assert 'No: and what is the exact deadline? [1]' in result['text']
    assert 'Approval is required.' in result['text'] and 'Emergency work is exempt.' in result['text']
    assert result['review_raw']==final and result['final_selected_decisions']==json.loads(final)
    assert result['kind_binding_sha256']==proposed['kind_binding_sha256']
    payload=json.loads(v.necessity_messages(p,first,c,raw)[1]['content'])
    assert list(payload)[-3:]==['proposal','fixed_question_kinds','kind_origin']


def test_final_crosskind_and_mapping_refusal():
    p,first,c,raw=inputs()
    for decision in ('no','not_applicable','mapping_unclear'):
        final=json.dumps({'F1':{'decision':decision,'sources':['C1:rule']},'F2':{'decision':'no','sources':['C1:rule']}})
        with pytest.raises(ValueError,match='Question mapping needs review' if decision=='mapping_unclear' else 'Invalid bound decision'):
            v.decode_review(final,p,first,c,raw)


def test_question_and_proposal_tamper_change_bound_hash():
    p,first,c,raw=inputs();one=v.decode_proposal(raw,p,first,c)
    changed=json.loads(raw);changed['F1']['question_kind']='supplied_text_yes_no'
    two=v.decode_proposal(json.dumps(changed),p,first,c)
    assert one['kind_binding_sha256']!=two['kind_binding_sha256']
    bad=deepcopy(p);bad['question']+='x'
    with pytest.raises(ValueError):v.decode_proposal(raw,bad,first,c)


def test_negative_polarity_and_mapping_to_cited_source_group():
    from potluck_local.services import investigation_request_parts as parts
    _,e,plans,_,_,_=old_fixture()
    question='Does the note not specify a deadline?'
    plan=parts.prepare(question,[question],origin='fixture_supplied',request=question)
    p=v.prepare(question,e,plan,request=question,unit_plans=plans);first=v.authored_inventory(p)
    c=proposal(question)
    raw=json.dumps({'F1':finding('supplied_text_yes_no','not_applicable')})
    final=json.dumps({'F1':{'decision':'yes','sources':['C1:rule']}})
    result=v.decode_review(final,p,first,c,raw)
    assert result['status']=='supported' and 'Yes: '+question+' [1]' in result['text']
    assert result['acknowledgements'][0]['source_unit_ids']==['C1:rule']
    assert 'Emergency work is exempt.' in result['text']
