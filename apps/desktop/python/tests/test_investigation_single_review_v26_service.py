import asyncio
import json
from copy import deepcopy
import pytest
import test_investigation_fixed_part_answers_service as shared
from potluck_local.services import investigations as inv

class SingleRuntime(shared.FixedRuntime):
    async def chat(self, messages, **kwargs):
        self.calls.append(deepcopy((messages, kwargs)))
        return {'choices':[{'message':{'content':json.dumps({'parts': {
            'P1': {'answer':'Approval is required.','verdict':'supported','evidence':['C1:rule'],'context':[]},
            'P2': {'answer':'Not established by the selected reviewed statements.','verdict':'unknown','evidence':[],'context':[]}}, 'context':[]})},'finish_reason':'stop'}]}

def test_single_lifecycle(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(shared, 'MODE', 'fixed-part-selection-v26')
        _, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        runtime = SingleRuntime(); monkeypatch.setattr(inv, 'get_runtime', lambda:runtime)
        monkeypatch.setattr(shared.routes, '_error', lambda exc: exc)
        item = await shared.create(scope)
        assert len(runtime.calls) == len(runtime.measurements) == 1
        assert runtime.calls[0][1]['max_tokens'] == 512
        p=item['provenance']; assert set(json.loads(p['reviewed_claim_selection']['raw'])) == {'review_raw'}
        assert 'reviewed_claim_selection_input' not in p and 'reviewed_claim_review_input' not in p
        assert p['input_budget']['output_tokens'] == 512
        assert p['input_budget']['version'] == 'fixed-part-single-review-v26'
        assert 'inventory' not in json.dumps(runtime.calls[0][0]).lower()
        schema=runtime.calls[0][1]['response_format']['schema']['properties']['parts']['properties']['P1']
        assert list(schema['properties'])==schema['required']==['evidence','context','verdict','answer']
        assert p['generation_settings']['max_tokens'] == 512
        assert p['generation_profile']['temperature'] == 0.7
        assert 'selection_raw' not in json.dumps(p['reviewed_claim_selection'])
        assert 'Two calls' not in p['factual_generation']
        assert (await inv.get_investigation(scope,item['id']))['brief'] == item['brief']
        await shared.routes.export(item['id'],scope.workspace,list(scope.collection_ids))
        await inv.review_investigation(scope,item['id'],'accept','reviewer','Synthetic acceptance')
        await shared.routes.export(item['id'],scope.workspace,list(scope.collection_ids))
    asyncio.run(run())

@pytest.mark.parametrize('target', ['raw', 'inventory', 'receipt', 'mode','mode_v23','messages','schema','input_binding','plan_binding','inventory_origin','profile','profile_thinking','snippet_binding','review_text','draft_tamper','forged_two_stage','authored_reply','authored_origin'])
def test_tampering_refused(tmp_path, monkeypatch, target):
    import sqlite3
    async def run():
        monkeypatch.setattr(shared, 'MODE', 'fixed-part-selection-v26')
        path, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        runtime=SingleRuntime(); monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        item=await shared.create(scope); p=deepcopy(item['provenance'])
        if target == 'raw': p['reviewed_claim_selection']['raw'] = json.dumps({'selection_raw':'{}','review_raw':'{}'})
        elif target == 'inventory': p['reviewed_claim_selection']['authored_inventory']['inventory']['C1:rule']=['P1:evidence']
        elif target == 'receipt': p['input_budget']['output_tokens']=2000
        elif target=='mode': p['presentation_mode']='fixed-part-selection-v22'
        elif target=='mode_v23': p['presentation_mode']='fixed-part-selection-v23'
        elif target=='messages': p['input_budget']['messages_sha256']='0'*64
        elif target=='schema': p['input_budget']['schema_sha256']='0'*64
        elif target=='input_binding': p['input_budget']['complete_input_sha256']='0'*64
        elif target=='plan_binding': p['input_budget']['question_part_plan_sha256']='0'*64
        elif target=='inventory_origin': p['input_budget']['inventory_origin']='model-reply'
        elif target=='profile': p['generation_profile']['name']='different-profile'
        elif target=='profile_thinking': p['generation_profile']['enable_thinking']=True
        elif target=='snippet_binding': p['reviewed_claim_selection']['snippets_sha256']='0'*64
        elif target=='review_text': p['reviewed_claim_selection']['review']['text']='Forged direct answer'
        elif target=='draft_tamper': p['reviewed_claim_selection']['review']['parts'][0]['draft_answer']='Forged answer'
        elif target=='forged_two_stage': p['reviewed_claim_review_input']=p['input_budget']
        elif target=='authored_reply': p['reviewed_claim_selection']['selection_raw']='{}'
        elif target=='authored_origin': p['reviewed_claim_selection']['authored_inventory']['origin']='model-reply'
        with sqlite3.connect(path) as db: db.execute('UPDATE investigations SET provenance=? WHERE id=?',(json.dumps(p),item['id']))
        with pytest.raises((ValueError, LookupError)): await inv.get_investigation(scope,item['id'])
    asyncio.run(run())

def test_cancel_before_reply_saves_nothing(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(shared, 'MODE', 'fixed-part-selection-v26')
        path, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        started=asyncio.Event()
        class Blocking(SingleRuntime):
            async def chat(self, messages, **kwargs):
                self.calls.append((messages,kwargs)); started.set(); await asyncio.Event().wait()
        runtime=Blocking(); monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        task=asyncio.create_task(shared.create(scope)); await started.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert len(runtime.calls)==1 and shared.counts(path)==(0,0)
    asyncio.run(run())

def test_correction_and_plan_supersession(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(shared, 'MODE', 'fixed-part-selection-v26')
        _, scope, claim, plan, old = await shared.setup(tmp_path, monkeypatch)
        runtime=SingleRuntime(); monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        item=await shared.create(scope)
        corrected=deepcopy(item['brief']); corrected['answers'][0]['text']='Approval is required.'
        reviewed=await inv.review_investigation(scope,item['id'],'correct','reviewer','Correction',corrected_brief=corrected)
        assert reviewed['brief']==item['brief']
        await shared.routes.export(item['id'],scope.workspace,list(scope.collection_ids))
        replacement=await shared.unit_plans.propose_plan('w',scope.collection_ids,claim,plan)
        await shared.unit_plans.supersede_plan('w',scope.collection_ids,old,replacement,'reviewer','Changed')
        with pytest.raises((ValueError, LookupError)): await inv.get_investigation(scope,item['id'])
    asyncio.run(run())

@pytest.mark.parametrize('measurement', [
    {'input_tokens':True,'context_tokens':8192}, {'input_tokens':120.5,'context_tokens':8192},
    {'context_tokens':8192}, {'input_tokens':120,'context_tokens':False},
    {'input_tokens':120,'context_tokens':8192.0}, {'input_tokens':120},
    {'input_tokens':100,'context_tokens':500}, {'input_tokens':7681,'context_tokens':8192},
])
def test_bad_measurement_never_dispatches(tmp_path, monkeypatch, measurement):
    async def run():
        monkeypatch.setattr(shared,'MODE','fixed-part-selection-v26')
        path,scope,*_=await shared.setup(tmp_path,monkeypatch)
        class Bad(SingleRuntime):
            async def measure_investigation_prompt(self,*a,**kw):
                self.measurements.append((a,kw)); return measurement
        runtime=Bad();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        with pytest.raises(ValueError): await shared.create(scope)
        assert not runtime.calls and shared.counts(path)==(0,0)
    asyncio.run(run())

@pytest.mark.parametrize('fault', ['length','invalid_verdict','foreign_id','extra_raw'])
def test_invalid_model_response_never_saves(tmp_path,monkeypatch,fault):
    async def run():
        monkeypatch.setattr(shared,'MODE','fixed-part-selection-v26')
        path,scope,*_=await shared.setup(tmp_path,monkeypatch)
        class Bad(SingleRuntime):
            async def chat(self,*a,**kw):
                reply=await super().chat(*a,**kw)
                if fault=='length': reply['choices'][0]['finish_reason']='length'
                else:
                    data=json.loads(reply['choices'][0]['message']['content'])
                    if fault=='invalid_verdict': data['parts']['P1']['verdict']='certain'
                    elif fault=='foreign_id': data['parts']['P1']['evidence']=['C99:foreign']
                    else: data['selection_raw']='{}'
                    reply['choices'][0]['message']['content']=json.dumps(data)
                return reply
        runtime=Bad();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        with pytest.raises(ValueError): await shared.create(scope)
        assert len(runtime.calls)==1 and shared.counts(path)==(0,0)
    asyncio.run(run())

@pytest.mark.parametrize('when',['measurement','reply'])
def test_model_identity_drift_rejected(tmp_path,monkeypatch,when):
    async def run():
        monkeypatch.setattr(shared,'MODE','fixed-part-selection-v26')
        path,scope,*_=await shared.setup(tmp_path,monkeypatch)
        class Drift(SingleRuntime):
            changed=False
            def current_model_sha256(self): return ('b' if self.changed else 'a')*64
            async def measure_investigation_prompt(self,*a,**kw):
                result=await super().measure_investigation_prompt(*a,**kw)
                if when=='measurement': self.changed=True
                return result
            async def chat(self,*a,**kw):
                result=await super().chat(*a,**kw)
                if when=='reply': self.changed=True
                return result
        runtime=Drift();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        with pytest.raises(ValueError): await shared.create(scope)
        assert len(runtime.calls)==1 and shared.counts(path)==(0,0)
    asyncio.run(run())

def test_plan_change_during_reply_fails_before_transaction(tmp_path,monkeypatch):
    async def run():
        monkeypatch.setattr(shared,'MODE','fixed-part-selection-v26')
        path,scope,claim,plan,old=await shared.setup(tmp_path,monkeypatch)
        class Changed(SingleRuntime):
            async def chat(self,*a,**kw):
                result=await super().chat(*a,**kw)
                replacement=await shared.unit_plans.propose_plan('w',scope.collection_ids,claim,plan)
                await shared.unit_plans.supersede_plan('w',scope.collection_ids,old,replacement,'reviewer','Changed')
                return result
        runtime=Changed();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        with pytest.raises(LookupError): await shared.create(scope)
        assert len(runtime.calls)==1 and shared.counts(path)==(0,0)
    asyncio.run(run())

@pytest.mark.parametrize('stage',['measure','transaction'])
def test_cancellation_rolls_back(tmp_path,monkeypatch,stage):
    from potluck_local.services import investigation_question_part_contracts as contracts
    async def run():
        monkeypatch.setattr(shared,'MODE','fixed-part-selection-v26')
        path,scope,*_=await shared.setup(tmp_path,monkeypatch); entered=asyncio.Event()
        class Block(SingleRuntime):
            async def measure_investigation_prompt(self,*a,**kw):
                if stage=='measure': entered.set(); await asyncio.Event().wait()
                return await super().measure_investigation_prompt(*a,**kw)
        runtime=Block();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        if stage=='transaction':
            original=contracts.persist_in_transaction
            async def block(*a,**kw):
                await original(*a,**kw); entered.set(); await asyncio.Event().wait()
            monkeypatch.setattr(contracts,'persist_in_transaction',block)
        task=asyncio.create_task(shared.create(scope)); await asyncio.wait_for(entered.wait(),2);task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert shared.counts(path)==(0,0) and len(runtime.calls)==(0 if stage=='measure' else 1)
    asyncio.run(run())

def test_source_revision_change_during_reply_fails_before_save(tmp_path,monkeypatch):
    from potluck_local.services.source_collections import scan_collection
    async def run():
        monkeypatch.setattr(shared,'MODE','fixed-part-selection-v26')
        path,scope,*_=await shared.setup(tmp_path,monkeypatch)
        class Changed(SingleRuntime):
            async def chat(self,*a,**kw):
                result=await super().chat(*a,**kw)
                (tmp_path/'docs'/'policy.md').write_text('Changed source revision.')
                await scan_collection('w',scope.collection_ids[0])
                return result
        runtime=Changed();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        with pytest.raises((ValueError, LookupError)): await shared.create(scope)
        assert len(runtime.calls)==1 and shared.counts(path)==(0,0)
    asyncio.run(run())

def test_context_capacity_drift_rejected_before_native_dispatch(tmp_path,monkeypatch):
    async def run():
        monkeypatch.setattr(shared,'MODE','fixed-part-selection-v26')
        path,scope,*_=await shared.setup(tmp_path,monkeypatch)
        class ContextDrift(SingleRuntime):
            dispatches=0
            async def chat(self,messages,**kw):
                assert kw['expected_context_tokens']==8192
                # Mirrors runtime.chat's loaded_n_ctx check, before engine dispatch.
                if kw['expected_context_tokens']!=4096: raise ValueError('context changed')
                self.dispatches+=1
                return await super().chat(messages,**kw)
        runtime=ContextDrift();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        with pytest.raises(ValueError): await shared.create(scope)
        assert runtime.dispatches==0 and shared.counts(path)==(0,0)
    asyncio.run(run())
