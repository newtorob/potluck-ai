import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.source_collections import register_collection, scan_collection


@pytest.mark.parametrize('bounded,unknown_mode,presentation_mode', [(False,None,None), (True,None,None), (True,'cited-unknown-v1',None), (True,'cited-unknown-v2',None), (True,'cited-unknown-v3',None), (True,'cited-unknown-v4',None), (True,'cited-unknown-v4','cited-conclusions-v1'),(True,'cited-unknown-v4','cited-conclusions-v2'),(True,'cited-unknown-v4','cited-conclusions-v3'),(True,'cited-unknown-v4','cited-conclusions-v4'),(True,'cited-unknown-v4','cited-conclusions-v5'),(True,'cited-unknown-v4','cited-conclusions-v6'),(True,'cited-unknown-v4','cited-conclusions-v7'),(True,'cited-unknown-v4','cited-components-v1')])
def test_formula_service_correction_export_and_tamper_detection(tmp_path, monkeypatch, bounded, unknown_mode, presentation_mode):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH',str(tmp_path/'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR',str(tmp_path))
    monkeypatch.setattr(inv,'get_settings',lambda:SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True),memory=SimpleNamespace(enabled=True)))
    calls=[]
    class Runtime:
        def current_model_id(self):return 'model'
        def current_model_sha256(self):return 'a'*64
        async def validate_investigation_profile(self,*a,**kw): pass
        async def measure_investigation_prompt(self,*a,**kw): return {'input_tokens': 1000, 'context_tokens': 8192}
        async def chat(self,messages,**kwargs):
            import jsonschema
            d=json.loads(messages[1]['content']);calls.append(d)
            assert kwargs['max_tokens']==2000 and kwargs['bounded_generation']
            formula=d['formula_ratio']['formulas'][0]
            user=d['formula_ratio']['user_inputs'];assert [x['value'] for x in user]==['8','12','3','36']
            half=next(q for q in d['quantities'] if q['unit']=='h')
            def inputs(count,age):return [{'variable':'count','input_id':count},{'variable':'age','input_id':age},{'variable':'half_life','input_id':half['id']}]
            response={'R1':{'formula_id':formula['id'],'part_inputs':inputs('U3','U4'),'total_inputs':inputs('U1','U2')},
                      'R2':{'groups':[{'focus':'scope','snippet_ids':[d['snippets'][-1]['snippet_id']],'status':'supported'}],'more':False}}
            if unknown_mode:
                response['R2']['groups'][0]['unknown_reason']=''
            if presentation_mode:
                response['R2']['groups'][0]['conclusion']='The scope is per account.'
            if presentation_mode == 'cited-components-v1':
                response['R2']['groups'][0].update(conditions=[], exceptions=[], distinctions=[])
            from potluck_local.services.runtime import _structured_response_format
            from potluck_local.services.split_native import SplitModel
            _structured_response_format(object.__new__(SplitModel),kwargs['response_format'])
            jsonschema.validate(response,kwargs['response_format']['schema'])
            return {'choices':[{'message':{'content':json.dumps(response)}}]}
    monkeypatch.setattr(inv,'get_runtime',Runtime)
    async def run():
        await init_memory_db();folder=tmp_path/'docs';folder.mkdir()
        (folder/'a.md').write_text('# Weight\n\nweight = count * 2^(-age / half_life)\n\nHalf-life is 12 h.\n\nScope is per account.\n')
        cid=await register_collection('w','docs',folder);await scan_collection('w',cid);scope=EvidenceScope('w',(cid,))
        item=await inv.create_investigation(scope,'Compare count 8 at 12 hours and count 3 at 36 hours.','weight half-life scope','model',
          presentation_mode=presentation_mode, unknown_mode=unknown_mode, reliability_mode='bounded-evidence-v1' if bounded else None, generation_profile='qwen3.8-27b-native-non-thinking' if bounded else None,
          template_version='investigation-v21',prompt_encoding='compact-calculation-v1',formula_mode='source-formula-ratio-v1',
          reviewed_operations=[{'request':'Using the formula, older weight as percentage of newer?','operation':'percentage'},
                               {'request':'Explain scope.','operation':'answer'}])
        assert len(calls)==1
        assert '9.375%' in item['brief']['answers'][0]['text']
        assert item['brief']['answers'][0]['status']=='supported'
        original=deepcopy(item)
        reviewed=await inv.review_investigation(scope,item['id'],'correct','fixture','diagnostic only',corrected_brief=item['brief'])
        assert reviewed['brief']==original['brief'] and reviewed['provenance']==original['provenance']
        assert reviewed['review']['corrected_brief']['answers'][0]['text']==original['brief']['answers'][0]['text']
        from potluck_local.routers.investigations import export
        markdown=(await export(item['id'],'w',[cid]))['markdown']
        assert 'Original source formula calculations' in markdown and 'user scenario' in markdown
        if presentation_mode:
            assert 'Original cited conclusions' in markdown and 'The scope is per account' in markdown
        assert '9\\.375%' in markdown and '0\\.375' in markdown
        # Rebuild checks reject mutation of the stored machine result, not owner edits.
        from potluck_local.services.investigation_source_formulas import validate_original
        tampered=deepcopy(item);tampered['provenance']['formula_calculations']['proposals']['R1']['part_inputs'][0]['input_id']='U1'
        with pytest.raises(ValueError):validate_original(tampered,item['provenance']['snippet_catalog'])
    asyncio.run(run())
