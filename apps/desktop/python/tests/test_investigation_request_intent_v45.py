"""Mocked property/scope/replay contracts; no model correctness credit."""
import asyncio,json,sqlite3
from copy import deepcopy
import pytest
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import investigation_part_selection_v45 as sel
from potluck_local.services import investigation_fixed_part_answers_v45 as adapter
from potluck_local.services import investigation_reviewed_evidence as evidence
from potluck_local.services import investigation_readable_presentation as readable
from potluck_local.services import investigations as inv
from test_investigation_reviewed_collection_v7 import setup_case,prepared
from test_investigation_fixed_part_answers_service import FixedRuntime


def fixture(q,texts):
    claims=[{'kind':'reviewed_claim','status':'approved','citation_id':f'claim:{i}','content':t,'sources':[{'citation_id':f'source:{i}','content':t}]} for i,t in enumerate(texts,1)]
    plan=parts.prepare(q,[q],origin='fixture_supplied',request=q)
    p=sel.prepare(q,claims,plan,request=q)
    return claims,plan,p,sel.authored_inventory(p)


def test_refinement_preserves_native_partition_parent_and_every_character():
    q='Do these notes state a size? If not, what import test and access exception do they describe?'
    claims,plan,p,first=fixture(q,['A test is described.','The exception requires approval.'])
    clause=json.dumps({'parts':{'P1':['Do these notes state a size?',' If not, what import test and access exception do they describe?']}})
    result=sel.decode_clause_proposal(clause,p,first)
    assert len(result['native_parts']['P1'])==2 and len(result['parts']['P1'])==3
    assert ''.join(result['parts']['P1'])==q and result['raw']==clause
    payload=json.loads(sel.review_messages(p,first,clause)[1]['content'])
    assert [f.get('answer_form') for f in payload['facts']]==['selected_text_availability_yes_no','description_request','description_request']
    assert all(f['subject_reference']=='unresolved_pronoun' for f in payload['facts'][1:])
    assert all(f['parent_request']==' If not, what import test and access exception do they describe?' for f in payload['facts'][1:])
    raw=json.dumps({'F1':{'decision':'no','sources':['C1:whole']},'F2':{'decision':'established','sources':['C1:whole']},'F3':{'decision':'established','sources':['C2:whole']}})
    final=sel.decode_review(raw,p,first,clause,raw)
    assert final['final_selected_decisions']==json.loads(raw)
    assert final['availability_scopes']['F1']['model_evidence_unit_ids']==['C1:whole']
    assert final['availability_scopes']['F1']['scope_unit_ids']==['C1:whole','C2:whole']
    assert not final['availability_scopes']['F1']['interpretation_verified']
    snippets,_=evidence.snippets(claims)
    brief,trace=adapter.make_brief(q,{'id':'q','request':q},claims,snippets,json.dumps({'clause_raw':clause,'selection_raw':raw,'review_raw':raw}),question_part_plan=plan)
    assert trace['clause_proposal']['raw']==clause and len(trace['clause_proposal']['parts']['P1'])==3
    assert brief['answers'][0]['passage_presentation']['availability_scopes']==final['availability_scopes']


@pytest.mark.parametrize('decision',['yes','no'])
def test_complete_scope_never_changes_native_polarity_or_extra_factual_citations(decision):
    q='Do these selected notes state a price?';_,_,p,first=fixture(q,['The price is 17 dollars.','An unrelated note.'])
    clause=json.dumps({'parts':{'P1':[q]}});raw=json.dumps({'F1':{'decision':decision,'sources':['C1:whole']}})
    result=sel.decode_review(raw,p,first,clause,raw);ack=result['acknowledgements'][0]
    assert ack['answer']==decision.capitalize() and ack['source_unit_ids']==['C1:whole']
    assert ack['scope_unit_ids']==['C1:whole','C2:whole'] and result['final_selected_decisions']==json.loads(raw)


def test_unknown_execution_keeps_known_description_without_text_availability_scope():
    q='What access rule do these notes describe, and did the lead sign?';_,_,p,first=fixture(q,['The lead must sign before access.'])
    clause=json.dumps({'parts':{'P1':['What access rule do these notes describe,',' and did the lead sign?']}})
    raw=json.dumps({'F1':{'decision':'established','sources':['C1:whole']},'F2':{'decision':'not_established','sources':['C1:whole']}})
    result=sel.decode_review(raw,p,first,clause,raw)
    assert result['status']=='unknown' and result['availability_scopes']=={}
    assert [r['text'] for r in result['unresolved_requests']]==[' and did the lead sign?']
    assert 'The lead must sign before access.' in result['text']


def test_task_layout_preserves_separate_decision_scope_and_factual_citation():
    from test_investigation_task_readable_integration import record
    claims=deepcopy(record()['evidence']);other=deepcopy(claims[0]);other['citation_id']='claim:2';claims.append(other)
    for index,claim in enumerate(claims):
        if index:
            claim['content']=claim['content'].replace('TASK-12','TASK-13').replace('Tasks/T.md','Tasks/U.md')
        for source in claim['sources']:
            source['citation_id']='source:'+str(index)+':'+source['chunk_id']+':r'
            if index:
                source['document_id']='d2';source['relative_path']='Tasks/U.md'
                source['content']=source['content'].replace('TASK-12','TASK-13')
                source['heading']=source['heading'].replace('TASK-12','TASK-13')
    q='Do these notes state an expiry date?';plan=parts.prepare(q,[q],origin='fixture_supplied',request=q)
    p=sel.prepare(q,claims,plan,request=q);first=sel.authored_inventory(p)
    clause=json.dumps({'parts':{'P1':[q]}});raw=json.dumps({'F1':{'decision':'no','sources':['C1:whole']}});snippets,_=evidence.snippets(claims)
    brief,_=adapter.make_brief(q,{'id':'q','request':q},claims,snippets,json.dumps({'clause_raw':clause,'selection_raw':raw,'review_raw':raw}),question_part_plan=plan)
    ans=brief['answers'][0];view=readable.project(ans,claims,question=q)
    assert view and 'scope for this answer (input, not verified interpretation): [1] [2]' in view['markdown']
    assert view['original_text']==ans['text'] and ans['passage_presentation']['acknowledgements'][0]['source_unit_ids']==['C1:whole']


def test_refined_partition_and_scope_persist_reopen_export_and_reject_tamper(setup_case,monkeypatch):
    from potluck_local.services.memory_db import get_memory_db_path
    from potluck_local.routers import investigations as routes
    async def run():
        scope,_,_=await prepared(setup_case);q='Do these notes state a date? If not, what rule and exception do they describe?'
        class Runtime(FixedRuntime):
            async def chat(self,messages,**kwargs):
                self.calls.append(deepcopy((messages,kwargs)));payload=json.loads(messages[1]['content'])
                if len(self.calls)==1:raw=json.dumps({'parts':{'P1':['Do these notes state a date?',' If not, what rule and exception do they describe?']}})
                else:
                    assert len(payload['facts'])==3
                    raw=json.dumps({'F1':{'decision':'no','sources':['C1:whole']},'F2':{'decision':'established','sources':['C1:whole']},'F3':{'decision':'established','sources':['C2:whole']}})
                return {'choices':[{'message':{'content':raw},'finish_reason':'stop'}]}
        runtime=Runtime();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        item=await inv.create_investigation(scope,q,q,'model',template_version='investigation-v21',reviewed_operations=[{'request':q,'operation':'answer'}],evidence_selection='reviewed-collection-v7',presentation_mode='fixed-part-selection-v45',generation_profile='qwen3.8-27b-native-non-thinking',prompt_encoding='compact-calculation-v1',reliability_mode='bounded-evidence-v1',question_part_plan=parts.prepare(q,[q],origin='fixture_supplied',request=q))
        assert len(runtime.calls)==3 and [v[1]['max_tokens'] for v in runtime.calls]==[256,512,512]
        assert item['provenance']['input_budget']['version']=='fixed-part-request-intent-audit-v45'
        assert (await inv.get_investigation(scope,item['id']))['provenance']==item['provenance']
        markdown=(await routes.export(item['id'],scope.workspace,list(scope.collection_ids)))['markdown']
        assert '2 of 2 selected indexed documents' in markdown and 'Selected-source scope for this answer' in markdown
        bad=deepcopy(item['brief']);bad['answers'][0]['passage_presentation']['availability_scopes']['F1']['scope_unit_ids'].pop()
        with sqlite3.connect(get_memory_db_path()) as db:db.execute('UPDATE investigations SET brief=? WHERE id=?',(json.dumps(bad),item['id']))
        with pytest.raises((ValueError,LookupError)):await inv.get_investigation(scope,item['id'])
    asyncio.run(run())


def test_candidate_refuses_query_selected_scope_before_runtime(monkeypatch):
    monkeypatch.setattr(inv,'get_runtime',lambda:pytest.fail('No runtime access'))
    with pytest.raises(ValueError,match='complete selected-document input'):
        asyncio.run(inv.create_investigation(None,'Do these notes state a date?','date','model',presentation_mode='fixed-part-selection-v45',evidence_selection='reviewed-current-v1'))


def test_refinements_respect_existing_total_clause_cap():
    phrase='What rule and exception do these notes describe?'
    q=' '.join([phrase]*6);_,_,p,first=fixture(q,['One statement.'])
    raw=json.dumps({'parts':{'P1':[phrase]*6}})
    with pytest.raises(ValueError,match='Question mapping needs review'):
        sel.decode_clause_proposal(raw,p,first)
