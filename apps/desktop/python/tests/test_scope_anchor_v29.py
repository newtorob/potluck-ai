"""Structural source-scope guard checks; no semantic correctness credit."""
import json,asyncio
from copy import deepcopy
import pytest
from test_investigation_single_review_v28 import prepared as old_prepared,wire
from potluck_local.services import investigation_part_selection_v29 as v
from potluck_local.services import investigation_fixed_part_answers_v29 as adapter
from potluck_local.services import investigation_reviewed_evidence

def prepared():
    q,ev,plans,part,_=old_prepared()
    return q,ev,plans,part,v.prepare(q,ev,part,unit_plans=plans)

def test_unanchored_proposal_can_reach_planned_review_but_final_fails_closed():
    *_,p=prepared();first=v.authored_inventory(p);raw=wire('unknown',[])
    proposal=v.decode_proposal(raw,p,first)
    assert proposal['scope_anchor_gate_applied'] is False
    assert json.loads(v.necessity_messages(p,first,raw)[1]['content'])['proposed_selection']==json.loads(raw)
    with pytest.raises(ValueError,match='scope anchor'):v.decode_review(raw,p,first)

@pytest.mark.parametrize('role',['evidence','safeguards','background_only'])
def test_anchor_has_to_be_visible_with_declared_dependencies(role):
    *_,p=prepared();first=v.authored_inventory(p);raw=json.loads(wire('unknown',[]))
    if role=='evidence':raw['parts']['P1']['evidence']=['C1:rule']
    elif role=='safeguards':raw['safeguards']=['C1:rule']
    else:raw['parts']['P1']['background']=['C1:rule']
    if role=='background_only':
        with pytest.raises(ValueError,match='scope anchor'):v.decode_review(json.dumps(raw),p,first)
    else:
        result=v.decode_review(json.dumps(raw),p,first)
        assert result['status']=='unknown' and result['coverage']=='not_verified'
        assert 'Approval is required.' in result['text'] and 'Emergency work is exempt.' in result['text']
        assert result['scope_anchor_gate_applied'] is True

def test_mixed_known_unknown_keeps_existing_scope_without_extra_units():
    *_,p=prepared();result=v.decode_review(wire('supported',['C1:rule']),p,v.authored_inventory(p))
    assert result['status']=='unknown' and result['parts'][0]['verdict']=='supported'
    assert result['parts'][1]['evidence_unit_ids']==[]

def test_two_raw_replies_replay_and_proposal_cannot_rescue_final_anchor():
    q,ev,plans,part,p=prepared();first=v.authored_inventory(p);unanchored=wire('unknown',[]);anchored=wire('unknown',['C1:rule'])
    snippets,_=investigation_reviewed_evidence.snippets(ev);op={'id':'q','request':q}
    brief,trace=adapter.make_brief(q,op,ev,snippets,json.dumps({'selection_raw':unanchored,'review_raw':anchored}),question_part_plan=part,unit_plans=plans)
    assert trace['proposal']['scope_anchor_gate_applied'] is False and trace['review']['scope_anchor_gate_applied'] is True
    assert 'Scope basis:' in brief['answers'][0]['text']
    with pytest.raises(ValueError,match='scope anchor'):
        adapter.make_brief(q,op,ev,snippets,json.dumps({'selection_raw':anchored,'review_raw':unanchored}),question_part_plan=part,unit_plans=plans)

@pytest.mark.parametrize('final_anchor',[True,False])
def test_actual_two_stage_service_defers_gate_and_persists_only_anchored_final(tmp_path,monkeypatch,final_anchor):
    import test_investigation_fixed_part_answers_service as shared
    from potluck_local.services import investigations as inv
    class Runtime(shared.FixedRuntime):
        async def chat(self,messages,**kw):
            self.calls.append(deepcopy((messages,kw)))
            ids=['C1:rule'] if final_anchor and len(self.calls)==2 else []
            return {'choices':[{'message':{'content':wire('unknown',ids)},'finish_reason':'stop'}]}
    async def run():
        monkeypatch.setattr(shared,'MODE','fixed-part-selection-v29')
        monkeypatch.setattr(shared.routes,'_error',lambda e:e)
        path,scope,*_=await shared.setup(tmp_path,monkeypatch);runtime=Runtime();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        if not final_anchor:
            with pytest.raises(ValueError,match='scope anchor'):await shared.create(scope)
            assert shared.counts(path)==(0,0)
        else:
            item=await shared.create(scope)
            assert item['provenance']['input_budget']['version']=='fixed-part-necessity-review-v29'
            assert item['provenance']['reviewed_claim_selection']['proposal']['scope_anchor_gate_applied'] is False
            assert (await inv.get_investigation(scope,item['id']))['brief']==item['brief']
            await shared.routes.export(item['id'],scope.workspace,list(scope.collection_ids))
        assert len(runtime.calls)==2 and all(row[1]['max_tokens']==256 for row in runtime.calls)
    asyncio.run(run())
