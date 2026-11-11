import asyncio,json,sqlite3
from copy import deepcopy
import pytest
import test_investigation_fixed_part_answers_service as shared
from potluck_local.services import investigations as inv
class Runtime(shared.FixedRuntime):
    async def chat(self,messages,**kwargs):
        self.calls.append(deepcopy((messages,kwargs)))
        return {'choices':[{'message':{'content':json.dumps({'parts':{'P1':{'evidence':['C1:rule'],'background':[],'verdict':'supported'},'P2':{'evidence':[],'background':[],'verdict':'unknown'}},'safeguards':[]})},'finish_reason':'stop'}]}
async def setup(tmp_path,monkeypatch,runtime):
    monkeypatch.setattr(shared,'MODE','fixed-part-selection-v28')
    monkeypatch.setattr(shared.routes,'_error',lambda exc:exc)
    path,scope,*_=await shared.setup(tmp_path,monkeypatch)
    monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
    return path,scope

def test_complete_two_stage_lifecycle(tmp_path,monkeypatch):
    async def run():
        runtime=Runtime();path,scope=await setup(tmp_path,monkeypatch,runtime)
        item=await shared.create(scope);p=item['provenance'];b=p['input_budget']
        assert len(runtime.calls)==len(runtime.measurements)==2
        assert [row[1]['max_tokens'] for row in runtime.calls]==[256,256]
        assert all(row[1]['expected_context_tokens']==8192 for row in runtime.calls)
        assert b['version']=='fixed-part-necessity-review-v28' and b['max_calls']==2 and b['max_total_output_tokens']==512
        assert [row['stage'] for row in b['stages']]==['proposal','necessity']
        trace=p['reviewed_claim_selection'];raw=json.loads(trace['raw'])
        assert set(raw)=={'selection_raw','review_raw'} and trace['proposal']['review_raw']==raw['selection_raw']
        assert json.loads(runtime.calls[1][0][1]['content'])['proposed_selection']==json.loads(raw['selection_raw'])
        assert (await inv.get_investigation(scope,item['id']))['brief']==item['brief']
        exported=await shared.routes.export(item['id'],scope.workspace,list(scope.collection_ids))
        assert 'Stage: proposal' in exported['markdown'] and 'Stage: necessity' in exported['markdown']
        await inv.review_investigation(scope,item['id'],'accept','reviewer','Synthetic only')
        await shared.routes.export(item['id'],scope.workspace,list(scope.collection_ids))
    asyncio.run(run())
@pytest.mark.parametrize('target',['proposal_raw','final_raw','proposal_trace','necessity_binding','first_hash','second_hash','proposal_hash','context_drift','output','call_ceiling','stage_order','single_envelope','inventory_origin'])
def test_tamper_refused(tmp_path,monkeypatch,target):
    async def run():
        runtime=Runtime();path,scope=await setup(tmp_path,monkeypatch,runtime);item=await shared.create(scope);p=deepcopy(item['provenance']);b=p['input_budget'];t=p['reviewed_claim_selection']
        if target in ('proposal_raw','final_raw','single_envelope'):
            raw=json.loads(t['raw'])
            if target=='single_envelope':raw.pop('selection_raw')
            else:raw['selection_raw' if target=='proposal_raw' else 'review_raw']='{}'
            t['raw']=json.dumps(raw)
        elif target=='proposal_trace':t['proposal']['text']='forged'
        elif target=='necessity_binding':t['necessity_input_sha256']='0'*64
        elif target=='first_hash':b['stages'][0]['messages_sha256']='0'*64
        elif target=='second_hash':b['stages'][1]['messages_sha256']='0'*64
        elif target=='proposal_hash':b['stages'][1]['proposal_raw_sha256']='0'*64
        elif target=='context_drift':b['stages'][1]['context_tokens']=16384
        elif target=='output':b['stages'][1]['output_tokens']=512
        elif target=='call_ceiling':b['max_calls']=3
        elif target=='stage_order':b['stages'].reverse()
        elif target=='inventory_origin':b['stages'][1]['inventory_origin']='model-reply'
        with sqlite3.connect(path) as db:db.execute('UPDATE investigations SET provenance=? WHERE id=?',(json.dumps(p),item['id']))
        with pytest.raises((ValueError,LookupError)):await inv.get_investigation(scope,item['id'])
    asyncio.run(run())
@pytest.mark.parametrize('stage',[1,2])
@pytest.mark.parametrize('fault',['length','invalid','measurement','cancel','context_drift'])
def test_failed_stage_never_saves_or_retries(tmp_path,monkeypatch,stage,fault):
    async def run():
        class Bad(Runtime):
            async def measure_investigation_prompt(self,*a,**kw):
                value=await super().measure_investigation_prompt(*a,**kw)
                if len(self.measurements)==stage:
                    if fault=='measurement':value['input_tokens']=8000
                    if fault=='context_drift':value['context_tokens']=False if stage==1 else 4096
                return value
            async def chat(self,*a,**kw):
                reply=await super().chat(*a,**kw)
                if len(self.calls)==stage:
                    if fault=='length':reply['choices'][0]['finish_reason']='length'
                    elif fault=='invalid':reply['choices'][0]['message']['content']='{}'
                    elif fault=='cancel':raise asyncio.CancelledError()
                return reply
        runtime=Bad();path,scope=await setup(tmp_path,monkeypatch,runtime)
        with pytest.raises((ValueError,asyncio.CancelledError)):await shared.create(scope)
        assert len(runtime.calls)==(stage-1 if fault in ('measurement','context_drift') else stage)
        assert shared.counts(path)==(0,0)
    asyncio.run(run())
