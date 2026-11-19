"""Mocked decisions test binding/presentation, not native model correctness."""
import hashlib
import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import investigation_reviewed_evidence as evidence
from potluck_local.services import investigation_part_selection_v42 as selection
from potluck_local.services import investigation_fixed_part_answers_v42 as adapter
from potluck_local.services import investigation_part_selection_v41 as old
from potluck_local.services.runtime import _structured_response_format
from potluck_local.services.split_native import SplitModel


def fixture(question,texts):
    claims=[{'kind':'reviewed_claim','citation_id':f'claim:{i}','content':text,
             'sources':[{'citation_id':f'source:{i}','content':text}]}
            for i,text in enumerate(texts,1)]
    plan=parts.prepare(question,[question],origin='fixture_supplied',request=question)
    prepared=selection.prepare(question,claims,plan,request=question)
    return claims,plan,prepared,selection.authored_inventory(prepared)


def flat(decision,ids=None):
    return json.dumps({'F1':{'decision':decision,'sources':ids or ['C1:whole']}})


@pytest.mark.parametrize('question,texts,proposed,final,ids,status,ack',[
 ('Do the selected notes specify a lease term?', ['The display resets every eight days. These notes specify no lease term.'], 'not_established','no',['C1:whole'],'supported','No'),
 ('May technicians photograph the test board?', ['Technicians wear gloves. This note gives no rule about photographing the test board.'], 'no','not_established',['C1:whole'],'unknown',None),
 ('Can the cabinet leave the building?', ['The cabinet must not leave the building.'], 'established','no',['C1:whole'],'supported','No'),
 ('Do the notes not specify a packing method?', ['The notes give no required packing method.'], 'no','yes',['C1:whole'],'supported','Yes'),
 ('Did the lead approve the first trial?', ['A trial starts only after the lead signs the form. The battery log has nine entries.'], 'no','not_established',['C1:whole'],'unknown',None),
 ('When does the library hold begin?', ['The library hold begins June 7.','The library hold begins June 10.'], 'established','incompatible',['C1:whole','C2:whole'],'conflicting',None),
 ('When may staff check out the instrument?', ['Checkout requires a signature and training. For an urgent test, training may be deferred, but the signature remains required before checkout.'], 'not_established','established',['C1:whole'],'supported',None),
])
def test_literal_final_decision_can_correct_proposal_without_type_constraint(question,texts,proposed,final,ids,status,ack):
    claims,plan,p,first=fixture(question,texts);c=json.dumps({'parts':{'P1':[question]}})
    proposal=flat(proposed);raw=flat(final,ids)
    payload=json.loads(selection.necessity_messages(p,first,c,proposal)[1]['content'])
    assert payload['proposal']==json.loads(proposal)
    assert not any(key in json.dumps(payload) for key in ('question_kind','source_answer','substantive_finding','fixed_question_kinds'))
    result=selection.decode_review(raw,p,first,c,proposal)
    assert result['status']==status and all(text in result['text'] for text in texts)
    assert result['final_selected_decisions']==json.loads(raw)
    assert selection.decode_proposal(proposal,p,first,c)['findings']==json.loads(proposal)
    if ack:
        row=result['acknowledgements'][0]
        assert row['answer']==ack and row['clause']==question and row['source_unit_ids']==ids
        assert 'Direct answers (selected reviewed statements; unverified)' in result['text']
        assert row['assessment']=='unverified model answer; application-authored acknowledgement'
    else:
        assert result['acknowledgements']==[]
    if status=='unknown':assert [x['text'] for x in result['unresolved_requests']]==[question]
    if status=='conflicting':assert 'no authoritative winner' in result['text']
    snippets,_=evidence.snippets(claims)
    brief,trace=adapter.make_brief(question,{'id':'q','request':question},claims,snippets,
        json.dumps({'clause_raw':c,'selection_raw':proposal,'review_raw':raw}),question_part_plan=plan)
    assert brief['answers'][0]['status']==status and trace['proposal']['raw']==proposal
    assert {x['citation_id'] for x in brief['answers'][0]['source_excerpts']}=={f'claim:{i}' for i in range(1,len(texts)+1)}


def test_frozen_native_contract_and_all_stage_schemas():
    assert hashlib.sha256(selection.DRAFT.encode()).hexdigest()=='b8dc42a7481c39460c327ef2aee843a425cf1fc0624efeee934a640e36fcc00f'
    assert hashlib.sha256(selection.AUDIT.encode()).hexdigest()=='38899d0b8844d6a8d09236f76d8b8edcad8ce6b40845ec057d569ee56bf0eb25'
    q='Only after approval, what applies, and what is the exact expiry date?'
    text='Approval is required. During an incident, training may be deferred, but approval remains required.'
    claims,plan,p,first=fixture(q,[text]);boundary=q.index('and what');clauses=[q[:boundary],q[boundary:]]
    c=json.dumps({'parts':{'P1':clauses}});proposal=json.dumps({'F1':{'decision':'established','sources':['C1:whole']},'F2':{'decision':'not_established','sources':['C1:whole']}})
    assert 'sources' not in json.loads(selection.clause_messages(p,first)[1]['content'])
    for messages in [selection.review_messages(p,first,c),selection.necessity_messages(p,first,c,proposal)]:
        payload=json.loads(messages[1]['content']);assert payload['question']==q
        assert ''.join(x['request'] for x in payload['facts'])==q
        assert payload['parts']==[{'id':'P1','request':q}]
        assert payload['sources']==[{'id':'C1:whole','text':text}]
    for schema in [selection.clause_schema(p,first),selection.review_schema(p,first,c),selection.necessity_schema(p,first,c,proposal)]:
        fmt={'type':'json_object','schema':schema};assert _structured_response_format(object.__new__(SplitModel),fmt)==fmt
    assert selection.review_schema(p,first,c)==selection.necessity_schema(p,first,c,proposal)
    assert adapter.clause_receipt(p,first)['model_visible_claim_ids']==[]
    assert adapter.review_receipt(p,first,c)['model_visible_claim_ids']==['C1']


@pytest.mark.parametrize('row',[
 {'decision':'unknown','sources':['C1:whole']},
 {'decision':'no','sources':['C9:whole']},
 {'decision':'no','sources':['C1:whole'],'question_kind':'supplied_text_yes_no'},
 {'decision':'no','sources':[]},
])
def test_old_types_bad_decisions_and_sources_refuse(row):
    q='May the cabinet open?';_,_,p,first=fixture(q,['The cabinet must remain closed.']);c=json.dumps({'parts':{'P1':[q]}})
    with pytest.raises(ValueError):selection.decode_proposal(json.dumps({'F1':row}),p,first,c)


def test_mapping_refusal_duplicate_keys_and_input_tamper():
    q='May the cabinet open?';_,_,p,first=fixture(q,['The cabinet must remain closed.']);c=json.dumps({'parts':{'P1':[q]}});proposal=flat('mapping_unclear')
    assert selection.decode_proposal(proposal,p,first,c)['findings']['F1']['decision']=='mapping_unclear'
    with pytest.raises(ValueError,match='Question mapping needs review'):selection.decode_review(proposal,p,first,c,proposal)
    with pytest.raises(ValueError):selection.decode_proposal('{"F1":{"decision":"no","sources":["C1:whole"]},"F1":{"decision":"yes","sources":["C1:whole"]}}',p,first,c)
    bad=deepcopy(p);bad['claims'][0]['text']+=' changed'
    with pytest.raises(ValueError):selection.review_messages(bad,first,c)
    with pytest.raises(ValueError):adapter.make_brief(q,{},[],[],'{}',question_part_plan={},_contract=old)
    with pytest.raises(ValueError,match='Invalid V42 contract'):adapter.validate_brief({},[],_contract=old)
