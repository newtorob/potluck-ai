"""Visible proposal is fallible source context, not correctness or approval."""
import asyncio,json
from copy import deepcopy
import pytest
from test_investigation_single_review_v28 import prepared as base_prepared,wire
from potluck_local.services import investigation_part_selection_v30 as v

def prepared():
    q,ev,plans,part,_=base_prepared()
    return v.prepare(q,ev,part,unit_plans=plans)

def test_visible_preview_keeps_evidence_dependencies_and_excludes_optional_background():
    p=prepared();first=v.authored_inventory(p);data=json.loads(wire('supported',['C1:rule']));data['parts']['P1']['background']=['C1:other'];raw=json.dumps(data)
    proposal=v.decode_proposal(raw,p,first);payload=json.loads(v.necessity_messages(p,first,raw)[1]['content'])
    preview=payload['proposed_visible_answer']
    assert preview=={'proposed_status':proposal['status'],'text':proposal['text'],'approval':'unverified'}
    assert 'Approval is required.' in preview['text'] and 'Emergency work is exempt.' in preview['text']
    assert 'No approval is required.' not in preview['text']
    assert payload['claims']==json.loads(v.review_messages(p,first)[1]['content'])['claims']
    assert 'No approval is required.' in json.dumps(payload['claims'])

def test_unanchored_proposal_preview_cannot_bypass_final_scope_guard():
    p=prepared();first=v.authored_inventory(p);raw=wire('unknown',[])
    payload=json.loads(v.necessity_messages(p,first,raw)[1]['content'])
    assert payload['proposed_visible_answer']['text']==v.decode_proposal(raw,p,first)['text']
    with pytest.raises(ValueError,match='scope anchor'):v.decode_review(raw,p,first)

def test_preview_recomputed_from_actual_first_reply_without_changing_catalog():
    p=prepared();first=v.authored_inventory(p)
    a=json.loads(v.necessity_messages(p,first,wire('supported',['C1:rule']))[1]['content'])
    b=json.loads(v.necessity_messages(p,first,wire('supported',['C1:other']))[1]['content'])
    assert a['claims']==b['claims'] and a['parts']==b['parts']
    assert a['proposed_visible_answer']!=b['proposed_visible_answer']
    assert a['proposed_selection']!=b['proposed_selection']

def test_actual_service_review_sees_visible_proposal_and_reopen_export_replays(tmp_path,monkeypatch):
    import test_investigation_fixed_part_answers_service as shared
    from potluck_local.services import investigations as inv
    class Runtime(shared.FixedRuntime):
        async def chat(self,messages,**kw):
            self.calls.append(deepcopy((messages,kw)))
            ids=['C1:rule','C1:other'] if len(self.calls)==1 else ['C1:rule']
            return {'choices':[{'message':{'content':wire('supported',ids)},'finish_reason':'stop'}]}
    async def run():
        monkeypatch.setattr(shared,'MODE','fixed-part-selection-v30');monkeypatch.setattr(shared.routes,'_error',lambda e:e)
        _,scope,*_=await shared.setup(tmp_path,monkeypatch);runtime=Runtime();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        item=await shared.create(scope)
        first=json.loads(runtime.calls[0][0][1]['content']);second=json.loads(runtime.calls[1][0][1]['content'])
        assert first['claims']==second['claims']
        assert 'Unrelated retention is 30 days.' in second['proposed_visible_answer']['text']
        assert 'Unrelated retention is 30 days.' not in item['brief']['answers'][0]['text']
        assert second['proposed_visible_answer']['approval']=='unverified'
        assert len(runtime.calls)==2 and all(row[1]['max_tokens']==256 for row in runtime.calls)
        assert item['provenance']['input_budget']['version']=='fixed-part-necessity-review-v30'
        assert (await inv.get_investigation(scope,item['id']))['brief']==item['brief']
        await shared.routes.export(item['id'],scope.workspace,list(scope.collection_ids))
    asyncio.run(run())
