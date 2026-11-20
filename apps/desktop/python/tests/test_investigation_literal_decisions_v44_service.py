"""Mocked persistence and replay; no native inference or quality score."""
import asyncio
from copy import deepcopy
import json
import sqlite3
import pytest
import test_investigation_fixed_part_answers_service as shared
from potluck_local.services import investigations as inv
from potluck_local.services import investigation_request_parts as parts
from potluck_local.routers import investigations as routes

@pytest.mark.parametrize('source_contents',[False,True])
def test_flat_create_reopen_correct_export_and_tamper(tmp_path,monkeypatch,source_contents):
    async def run():
        path,scope,_,_,_=await shared.setup(tmp_path,monkeypatch,approve_plan=False)
        first_question='Do the supplied notes specify an exact expiry date' if source_contents else 'May visitors reset the controller'
        q=first_question+', and what is required for access?';clauses=[first_question,', and what is required for access?']
        plan=parts.prepare(q,[q],origin='fixture_supplied',request=q)
        class Runtime(shared.FixedRuntime):
            async def chat(self,messages,**kwargs):
                self.calls.append(deepcopy((messages,kwargs)));payload=json.loads(messages[1]['content'])
                if len(self.calls)==1:raw=json.dumps({'parts':{'P1':clauses}})
                else:
                    uid=payload['sources'][0]['id'];decision=('yes' if source_contents else 'no') if len(self.calls)==2 else ('no' if source_contents else 'not_established')
                    raw=json.dumps({'F1':{'decision':decision,'sources':[uid]},'F2':{'decision':'established','sources':[uid]}})
                return {'choices':[{'message':{'content':raw},'finish_reason':'stop'}]}
        runtime=Runtime();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        item=await inv.create_investigation(scope,q,'Access','model',template_version='investigation-v21',reviewed_operations=[{'request':q,'operation':'answer'}],evidence_selection='reviewed-current-v1',presentation_mode='fixed-part-selection-v44',generation_profile='qwen3.8-27b-native-non-thinking',prompt_encoding='compact-calculation-v1',reliability_mode='bounded-evidence-v1',question_part_plan=plan)
        answer=item['brief']['answers'][0];assert answer['status']==('supported' if source_contents else 'unknown')
        assert 'Approval is required.' in answer['text'] and 'Suspended members are excluded.' in answer['text']
        if source_contents:assert 'No: '+first_question in answer['text'] and 'Direct answers' in answer['text']
        else:assert answer['passage_presentation']['unresolved_requests'][0]['text']==first_question
        assert len(runtime.calls)==3 and [c[1]['max_tokens'] for c in runtime.calls]==[256,512,512]
        assert not any(k in json.dumps([c[0] for c in runtime.calls]) for k in ['question_kind','source_answer','fixed_question_kinds'])
        budget=item['provenance']['input_budget'];assert budget['version']=='fixed-part-source-property-audit-v44' and budget['max_total_output_tokens']==1280
        assert [c['model_input_contract'] for c in budget['stages']]==['fixed-part-selection-v44']*3
        assert item['provenance']['generation_settings']['timeout_seconds']==330
        assert item['provenance']['evidence_selection']['limitation']=='Source review is recorded; entailment is not verified.'
        reopened=await inv.get_investigation(scope,item['id']);assert reopened['brief']==item['brief'] and reopened['provenance']==item['provenance']
        exported=(await routes.export(item['id'],scope.workspace,list(scope.collection_ids)))['markdown']
        original=(await routes.export(item['id'],scope.workspace,list(scope.collection_ids),layout='original'))['markdown']
        assert first_question in exported and 'Presented reviewed claim IDs (clauses): none' in original
        assert 'Presented reviewed claim IDs (proposal): C1' in original and 'Presented reviewed claim IDs (necessity): C1' in original
        altered=deepcopy(item['provenance']);altered['reviewed_claim_selection']['review']['final_selected_decisions']['F1']['decision']='yes'
        with sqlite3.connect(path) as db:db.execute('UPDATE investigations SET provenance=? WHERE id=?',(json.dumps(altered),item['id']))
        with pytest.raises((ValueError,LookupError)):await inv.get_investigation(scope,item['id'])
        with sqlite3.connect(path) as db:db.execute('UPDATE investigations SET provenance=? WHERE id=?',(json.dumps(item['provenance']),item['id']))
        bad=deepcopy(item['brief'])
        if source_contents:bad['answers'][0]['passage_presentation']['acknowledgements'][0]['answer']='Yes'
        else:bad['answers'][0]['passage_presentation']['unresolved_requests'][0]['text']='different request'
        with sqlite3.connect(path) as db:db.execute('UPDATE investigations SET brief=? WHERE id=?',(json.dumps(bad),item['id']))
        with pytest.raises((ValueError,LookupError)):await inv.get_investigation(scope,item['id'])
        with sqlite3.connect(path) as db:db.execute('UPDATE investigations SET brief=? WHERE id=?',(json.dumps(item['brief']),item['id']))
        corrected=deepcopy(item['brief']);corrected['answers'][0]['text']='The selected notes require approval and exclude suspended members; review the exact evidence.'
        reviewed=await inv.review_investigation(scope,item['id'],'correct','Fixture reviewer','Synthetic correction',corrected_brief=corrected)
        assert reviewed['brief']==item['brief']
        assert (await inv.get_investigation(scope,item['id']))['review']['corrected_brief']['answers'][0]['text']==corrected['answers'][0]['text']
        assert (await routes.export(item['id'],scope.workspace,list(scope.collection_ids)))['markdown']
    asyncio.run(run())
