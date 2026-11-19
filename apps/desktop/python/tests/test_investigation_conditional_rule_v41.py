"""Replay and source binding checks; mocked decisions are not model-quality proof."""
import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import investigation_reviewed_evidence as evidence
from potluck_local.services import investigation_part_selection_v40 as previous
from potluck_local.services import investigation_part_selection_v41 as selection
from potluck_local.services import investigation_fixed_part_answers_v41 as adapter


def prepared(question, texts):
    claims=[{'kind':'reviewed_claim','citation_id':f'claim:{i}','content':text,
             'sources':[{'citation_id':f'source:{i}','content':text}]}
            for i,text in enumerate(texts,1)]
    plan=parts.prepare(question,[question],origin='fixture_supplied',request=question)
    p=selection.prepare(question,claims,plan,request=question)
    return claims,plan,p,selection.authored_inventory(p)


def proposal(decisions):
    return json.dumps({f'F{i}':{'question_kind':'substantive_rule_or_value',
        'source_answer':'not_applicable','substantive_finding':decision,'sources':ids}
        for i,(decision,ids) in enumerate(decisions,1)})


def test_conditional_governing_rule_and_unproved_actual_facts_remain_separate():
    clauses=['Under the drill procedure, what must happen before opening the cabinet,',
             ' may the tool leave the room,',' and has the custodian actually signed?']
    q=''.join(clauses)
    rule=('The cabinet may open only after the custodian signs the log and identity is verified. '
          'For an urgent drill, identity verification may be deferred, but the signature is still required before opening.')
    other='The tool label is checked every seven days.'
    claims,plan,p,first=prepared(q,[rule,other])
    c=json.dumps({'parts':{'P1':clauses}})
    draft=proposal([('established',['C1:whole']),('not_established',['C1:whole','C2:whole']),('not_established',['C1:whole'])])
    final=json.dumps({'F1':{'decision':'established','sources':['C1:whole']},
                      'F2':{'decision':'not_established','sources':['C1:whole','C2:whole']},
                      'F3':{'decision':'not_established','sources':['C1:whole']}})
    payload=json.loads(selection.necessity_messages(p,first,c,draft)[1]['content'])
    assert payload['sources']==[{'id':u['id'],'text':u['text']} for u in p['catalog']['units']]
    assert payload['question']==q and ''.join(f['request'] for f in payload['facts'])==q
    result=selection.decode_review(final,p,first,c,draft)
    assert result['status']=='unknown'
    assert [x['text'] for x in result['unresolved_requests']]==clauses[1:]
    assert rule in result['text'] and other in result['text']
    assert result['final_selected_decisions']==json.loads(final)
    snippets,_=evidence.snippets(claims)
    brief,trace=adapter.make_brief(q,{'id':'q','request':q},claims,snippets,
        json.dumps({'clause_raw':c,'selection_raw':draft,'review_raw':final}),question_part_plan=plan)
    assert brief['answers'][0]['status']=='unknown'
    assert {s['citation_id'] for s in brief['answers'][0]['source_excerpts']}=={'claim:1','claim:2'}
    assert trace['review']['final_selected_decisions']==json.loads(final)


def test_incompatible_conditions_do_not_choose_a_rule_or_drop_a_source():
    q='Which seal must be used before the chamber opens?'
    texts=['Before the chamber opens, use a copper seal.','Before the chamber opens, use a ceramic seal.']
    _,_,p,first=prepared(q,texts);c=json.dumps({'parts':{'P1':[q]}})
    draft=proposal([('established',['C1:whole'])])
    final=json.dumps({'F1':{'decision':'incompatible','sources':['C1:whole','C2:whole']}})
    result=selection.decode_review(final,p,first,c,draft)
    assert result['status']=='conflicting' and all(t in result['text'] for t in texts)
    assert set(result['unit_ids'])=={'C1:whole','C2:whole'}
    assert 'no authoritative winner' in result['text']


def test_bound_question_and_source_refusals_remain_closed():
    q='May the chamber open?';_,_,p,first=prepared(q,['The chamber must remain closed.'])
    c=json.dumps({'parts':{'P1':[q]}});draft=proposal([('established',['C1:whole'])])
    for decision,ids in [('no',['C1:whole']),('mapping_unclear',['C1:whole']),('established',['C9:whole'])]:
        with pytest.raises(ValueError):selection.decode_review(json.dumps({'F1':{'decision':decision,'sources':ids}}),p,first,c,draft)
    damaged=deepcopy(p);damaged['claims'][0]['text']+=' changed'
    with pytest.raises(ValueError):selection.necessity_messages(damaged,first,c,draft)


def test_previous_payload_schema_and_proposal_contract_are_preserved():
    q='Which rule applies?';claims,plan,p,first=prepared(q,['Approval is required unless an emergency is declared.'])
    old=previous.prepare(q,claims,plan,request=q);old_first=previous.authored_inventory(old)
    c=json.dumps({'parts':{'P1':[q]}});draft=proposal([('established',['C1:whole'])])
    assert selection.DRAFT==previous.DRAFT and selection.AUDIT!=previous.AUDIT
    assert selection.clause_messages(p,first)==previous.clause_messages(old,old_first)
    assert selection.review_messages(p,first,c)==previous.review_messages(old,old_first,c)
    assert selection.necessity_schema(p,first,c,draft)==previous.necessity_schema(old,old_first,c,draft)
    assert selection.necessity_messages(p,first,c,draft)[1]==previous.necessity_messages(old,old_first,c,draft)[1]


@pytest.mark.parametrize('operation',['create','replay'])
def test_injected_older_contract_refuses_before_binding_or_save(operation):
    with pytest.raises(ValueError,match='Invalid V41 contract'):
        if operation=='create':
            adapter.make_brief('q',{},[],[],'{}',question_part_plan={},_contract=previous)
        else:
            adapter.validate_brief({},[],_contract=previous)
