import json
from copy import deepcopy
import jsonschema
import pytest
from potluck_local.services import investigation_answer_components_v4 as v4
from potluck_local.services import investigation_answer_presentation as presentation
from potluck_local.services import investigation_aspect_source_answer_v20 as base
from potluck_local.services import investigation_unknown_explanations as unknown
from potluck_local.services import investigation_structural_answer as structural

PLAN = [{'id':'R1','operation':'answer','request':'What is the policy?'}]
SNIPPETS = [{'snippet_id':f'S{i}','citation_id':f'C{i}','quote':f'Source {i} text.'} for i in range(1,4)]

def data():
    return {'R1': {'supported':[{'focus':'Policy','primary':{'text':'The rule applies.','snippet_ids':['S1']},'conditions':[{'text':'Approval is required.','snippet_ids':['S2']}],'exceptions':[]}], 'unknown':[], 'conflicting':[], 'more':False}}

def schema():
    value=unknown.response_schema(base.response_schema(PLAN,SNIPPETS,[]),PLAN,version='cited-unknown-v4')
    return presentation.response_schema(value,PLAN,version=v4.VERSION)


def test_runtime_schema_union_and_exact_replay():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    fmt={'type':'json_object','schema':schema()}; assert _structured_response_format(object.__new__(SplitModel),fmt)==fmt
    value=data();jsonschema.validate(value,schema())
    raw,original=v4.extract(json.dumps(value),PLAN,SNIPPETS)
    assert json.loads(raw)['R1']['groups'][0]['snippet_ids']==['S1','S2']
    assert original['R1'][0]['primary_snippet_ids']==['S1']
    brief,_,coverage=structural.decode_results(json.dumps(value),PLAN,SNIPPETS,[],[],structural_trace={'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v4.VERSION)
    assert 'Approval is required.' in brief['answers'][0]['text']
    structural.validate_original(brief,coverage,SNIPPETS,{'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v4.VERSION)
    changed=deepcopy(coverage);changed['cited_conclusions']['operations']['R1'][0]['primary_snippet_ids']=['S3']
    with pytest.raises(ValueError): structural.validate_original(brief,changed,SNIPPETS,{'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v4.VERSION)


def test_mixed_unknown_conflict_roundtrip():
    value=data();value['R1']['unknown']=[{'focus':'Missing','primary':{'text':'The date is not stated.','snippet_ids':[]},'distinctions':[]}]
    value['R1']['conflicting']=[{'focus':'Conflict','primary':{'text':'The sources disagree.','snippet_ids':['S2','S3']}}]
    brief,_,coverage=structural.decode_results(json.dumps(value),PLAN,SNIPPETS,[],[],structural_trace={'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v4.VERSION)
    assert 'The sources disagree.' in brief['answers'][0]['text']
    structural.validate_original(brief,coverage,SNIPPETS,{'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v4.VERSION)


@pytest.mark.parametrize('mutate',[
    lambda x:x['R1']['supported'][0]['primary'].update(snippet_ids=[]),
    lambda x:x['R1']['supported'][0]['primary'].update(snippet_ids=['S9']),
    lambda x:x['R1']['supported'][0]['primary'].update(extra='bad'),
    lambda x:x['R1']['supported'][0].update(snippet_ids=['S1']),
    lambda x:x['R1']['supported'][0]['conditions'][0].update(snippet_ids=['S9']),
    lambda x:x['R1']['supported'][0]['primary'].update(text='x'*161+'.'),
])
def test_invalid_statements_fail_closed(mutate):
    value=data();mutate(value)
    with pytest.raises(ValueError): v4.extract(json.dumps(value),PLAN,SNIPPETS)


def test_v4_supported_budget_900_and_unknown_300():
    value=data(); group=value['R1']['supported'][0]
    group['primary']['text']='A'*150+'.'
    group['conditions']=[{'text':'B'*150+'.','snippet_ids':['S2']}]
    brief,_,_=structural.decode_results(json.dumps(value),PLAN,SNIPPETS,[],[],structural_trace={'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v4.VERSION)
    assert group['conditions'][0]['text'] in brief['answers'][0]['text']
    value['R1']['supported']=[]
    value['R1']['unknown']=[{'focus':'Unknown','primary':{'text':'A'*150+'.','snippet_ids':['S1']},'distinctions':[{'text':'B'*150+'.','snippet_ids':['S2']}]}]
    with pytest.raises(ValueError): v4.extract(json.dumps(value),PLAN,SNIPPETS)


def test_primary_binding_tamper_with_same_union_rejected():
    value = data()
    brief, _, coverage = structural.decode_results(json.dumps(value), PLAN, SNIPPETS, [], [], structural_trace={'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v4.VERSION)
    assert 'Primary statement sources: [S1].' in brief['answers'][0]['text']
    coverage['cited_conclusions']['operations']['R1'][0]['primary_snippet_ids'] = ['S1', 'S2']
    with pytest.raises(ValueError):
        structural.validate_original(brief, coverage, SNIPPETS, {'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v4.VERSION)


@pytest.mark.parametrize('status', ['supported', 'unknown', 'conflicting'])
def test_reviewed_service_correction_and_verification_roundtrip(tmp_path, monkeypatch, status):
    import asyncio
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.services import investigation_verification as verification
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.source_collections import register_collection, scan_collection
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'collection.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    async def run():
        await init_memory_db(); folder = tmp_path / 'docs'; folder.mkdir()
        (folder / 'policy.md').write_text('# Policy\nManagers require prior approval before work.\n')
        cid = await register_collection('w', 'test', folder); await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,)); lookup = await lookup_evidence(scope, 'approval', 10, retrieval_version='v2')
        src = lookup['source_chunks'][0]; ref = {k:src[k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}
        claim = await propose_claim('w', scope.collection_ids, 'approval.rule', 'Managers require prior approval before work.', [ref])
        await approve_claim('w', scope.collection_ids, claim, 'test-reviewer', 'Test only')
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def validate_investigation_profile(self, *args, **kwargs): return None
            async def measure_investigation_prompt(self, *args, **kwargs): return {'input_tokens':1000, 'context_tokens':8192}
            async def chat(self, messages, **kwargs):
                prompt = json.loads(messages[1]['content'])
                sid = prompt['snippets'][0]['snippet_id']
                row = {'supported': [], 'unknown': [], 'conflicting': [], 'more': False}
                primary_text = 'Managers require prior approval before work.' if status == 'supported' else 'The approval time is not stated.' if status == 'unknown' else 'The supplied statements disagree on approval.'
                group = {'focus': 'Approval', 'primary': {'text': primary_text, 'snippet_ids': [sid]}}
                group.update({'conditions': [], 'exceptions': []} if status == 'supported' else {'distinctions': []} if status == 'unknown' else {})
                row[status].append(group)
                return {'choices': [{'message': {'content': json.dumps({'R1': row})}}]}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        item = await inv.create_investigation(scope, 'What approval applies?', 'approval', 'model', template_version='investigation-v21', generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1', reviewed_operations=[{'request':'What approval applies?', 'operation':'answer'}], reliability_mode='bounded-evidence-v1', unknown_mode='cited-unknown-v4', presentation_mode=v4.VERSION, evidence_selection='reviewed-collection-v2')
        assert item['brief']['answers'][0]['status'] == status
        for version in ('draft-verification-v1','draft-verification-v2','draft-verification-v3'):
            verification.original_input(item, version=version)
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'reviewer', 'Test correction', corrected_brief=deepcopy(item['brief']))
        assert reviewed['review']['corrected_brief']['answers'][0]['text'] == item['brief']['answers'][0]['text']
        assert reviewed['review']['corrected_brief']['answers'][0]['source_excerpts'] == item['brief']['answers'][0]['source_excerpts']
        assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
    asyncio.run(run())

