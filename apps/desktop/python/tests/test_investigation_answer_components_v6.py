import json
from copy import deepcopy
import jsonschema
import pytest
from potluck_local.services import investigation_answer_components_v6 as v6
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
    return presentation.response_schema(value,PLAN,version=v6.VERSION)


def test_runtime_schema_union_and_exact_replay():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    fmt={'type':'json_object','schema':schema()}; assert _structured_response_format(object.__new__(SplitModel),fmt)==fmt
    value=data();jsonschema.validate(value,schema())
    raw,original=v6.extract(json.dumps(value),PLAN,SNIPPETS)
    assert json.loads(raw)['R1']['groups'][0]['snippet_ids']==['S1','S2']
    assert original['R1'][0]['primary_snippet_ids']==['S1']
    brief,_,coverage=structural.decode_results(json.dumps(value),PLAN,SNIPPETS,[],[],structural_trace={'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v6.VERSION)
    assert 'Approval is required.' in brief['answers'][0]['text']
    structural.validate_original(brief,coverage,SNIPPETS,{'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v6.VERSION)
    changed=deepcopy(coverage);changed['cited_conclusions']['operations']['R1'][0]['primary_snippet_ids']=['S3']
    with pytest.raises(ValueError): structural.validate_original(brief,changed,SNIPPETS,{'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v6.VERSION)


def test_mixed_unknown_conflict_roundtrip():
    value=data();value['R1']['unknown']=[{'focus':'Missing','primary':{'text':'The date is not stated.','snippet_ids':[]},'distinctions':[]}]
    value['R1']['conflicting']=[{'focus':'Conflict','primary':{'text':'The sources disagree.','snippet_ids':['S2','S3']}}]
    brief,_,coverage=structural.decode_results(json.dumps(value),PLAN,SNIPPETS,[],[],structural_trace={'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v6.VERSION)
    assert 'The sources disagree.' in brief['answers'][0]['text']
    structural.validate_original(brief,coverage,SNIPPETS,{'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v6.VERSION)


@pytest.mark.parametrize('mutate',[
    lambda x:x['R1']['supported'][0]['primary'].update(snippet_ids=[]),
    lambda x:x['R1']['supported'][0]['primary'].update(snippet_ids=['S9']),
    lambda x:x['R1']['supported'][0]['primary'].update(extra='bad'),
    lambda x:x['R1']['supported'][0].update(snippet_ids=['S1']),
    lambda x:x['R1']['supported'][0]['conditions'][0].update(snippet_ids=['S9']),
    lambda x:x['R1']['supported'][0]['primary'].update(text='x'*300+'.'),
])
def test_invalid_statements_fail_closed(mutate):
    value=data();mutate(value)
    with pytest.raises(ValueError): v6.extract(json.dumps(value),PLAN,SNIPPETS)


def test_v6_supported_budget_900_and_unknown_300():
    value=data(); group=value['R1']['supported'][0]
    group['primary']['text']='A'*150+'.'
    group['conditions']=[{'text':'B'*150+'.','snippet_ids':['S2']}]
    brief,_,_=structural.decode_results(json.dumps(value),PLAN,SNIPPETS,[],[],structural_trace={'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v6.VERSION)
    assert group['conditions'][0]['text'] in brief['answers'][0]['text']
    value['R1']['supported']=[]
    value['R1']['unknown']=[{'focus':'Unknown','primary':{'text':'A'*150+'.','snippet_ids':['S1']},'distinctions':[{'text':'B'*150+'.','snippet_ids':['S2']}]}]
    with pytest.raises(ValueError): v6.extract(json.dumps(value),PLAN,SNIPPETS)


def test_primary_binding_tamper_with_same_union_rejected():
    value = data()
    brief, _, coverage = structural.decode_results(json.dumps(value), PLAN, SNIPPETS, [], [], structural_trace={'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v6.VERSION)
    assert 'Primary statement sources: [S1].' in brief['answers'][0]['text']
    coverage['cited_conclusions']['operations']['R1'][0]['primary_snippet_ids'] = ['S1', 'S2']
    with pytest.raises(ValueError):
        structural.validate_original(brief, coverage, SNIPPETS, {'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v6.VERSION)


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
                budget = prompt['answer_component_budget']
                assert budget['max_supported_primary_characters'] == 300
                assert budget['max_unknown_primary_characters'] == budget['max_conflicting_primary_characters'] == budget['max_component_characters'] == 160
                assert 'max_characters_per_field' not in budget
                assert kwargs['response_format']['schema']['properties']['R1']['properties']['supported']['items']['properties']['primary']['properties']['text']['maxLength'] == 300
                assert 'Supported primary is at most 300 characters' in messages[0]['content']
                row = {'supported': [], 'unknown': [], 'conflicting': [], 'more': False}
                primary_text = ('Managers require prior approval before work. ' + 'A' * 130 + '.') if status == 'supported' else 'The approval time is not stated.' if status == 'unknown' else 'The supplied statements disagree on approval.'
                group = {'focus': 'Approval', 'primary': {'text': primary_text, 'snippet_ids': [sid]}}
                group.update({'conditions': [{'text':'Prior approval required','snippet_ids':[sid]}], 'exceptions': []} if status == 'supported' else {'distinctions': [{'text':'Protocol rather than release','snippet_ids':[sid]}]} if status == 'unknown' else {})
                row[status].append(group)
                return {'choices': [{'message': {'content': json.dumps({'R1': row})}}]}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        item = await inv.create_investigation(scope, 'What approval applies?', 'approval', 'model', template_version='investigation-v21', generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1', reviewed_operations=[{'request':'What approval applies?', 'operation':'answer'}], reliability_mode='bounded-evidence-v1', unknown_mode='cited-unknown-v4', presentation_mode=v6.VERSION, evidence_selection='reviewed-collection-v2')
        assert item['brief']['answers'][0]['status'] == status
        if status == 'supported': assert 'Prior approval required' in item['brief']['answers'][0]['text']
        if status == 'unknown': assert 'Protocol rather than release' in item['brief']['answers'][0]['text']
        for version in ('draft-verification-v1','draft-verification-v2','draft-verification-v3'):
            verification.original_input(item, version=version)
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'reviewer', 'Test correction', corrected_brief=deepcopy(item['brief']))
        assert reviewed['review']['corrected_brief']['answers'][0]['text'] == item['brief']['answers'][0]['text']
        assert reviewed['review']['corrected_brief']['answers'][0]['source_excerpts'] == item['brief']['answers'][0]['source_excerpts']
        assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
    asyncio.run(run())



def test_clause_is_verbatim_in_display_and_replay_but_v4_rejects():
    from potluck_local.services import investigation_answer_components_v4 as old
    value=data(); value['R1']['supported'][0]['conditions'][0]['text']='Package damaged in transit'
    with pytest.raises(ValueError): old.extract(json.dumps(value),PLAN,SNIPPETS)
    brief,_,coverage=structural.decode_results(json.dumps(value),PLAN,SNIPPETS,[],[],structural_trace={'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v6.VERSION)
    assert 'Package damaged in transit' in brief['answers'][0]['text']
    assert coverage['cited_conclusions']['operations']['R1'][0]['components']['conditions'][0]['text']=='Package damaged in transit'
    structural.validate_original(brief,coverage,SNIPPETS,{'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v6.VERSION)


@pytest.mark.parametrize('text',['','unfinished\nline','X'*161,'Clause [S1]', 'Clause [s1]'])
def test_unsafe_or_invalid_clause_rejected(text):
    value=data();value['R1']['supported'][0]['conditions'][0]['text']=text
    with pytest.raises(ValueError): v6.extract(json.dumps(value),PLAN,SNIPPETS)


def test_primary_still_requires_terminal_punctuation():
    value=data();value['R1']['supported'][0]['primary']['text']='Incomplete primary'
    with pytest.raises(ValueError): v6.extract(json.dumps(value),PLAN,SNIPPETS)


def test_multiple_clauses_have_clear_boundaries_and_exact_replay():
    value = data()
    value['R1']['supported'][0]['conditions'] = [
        {'text': 'Prior approval required', 'snippet_ids': ['S1']},
        {'text': 'Available during staffed hours', 'snippet_ids': ['S2']},
    ]
    brief, _, coverage = structural.decode_results(json.dumps(value), PLAN, SNIPPETS, [], [],
        structural_trace={'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v6.VERSION)
    assert 'Conditions: Prior approval required; Available during staffed hours' in brief['answers'][0]['text']
    components = coverage['cited_conclusions']['operations']['R1'][0]['components']['conditions']
    assert [item['text'] for item in components] == ['Prior approval required', 'Available during staffed hours']
    structural.validate_original(brief, coverage, SNIPPETS, {'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v6.VERSION)


def test_unknown_clauses_use_separator_and_preserve_primary():
    value = data(); value['R1']['supported'] = []
    value['R1']['unknown'] = [{'focus': 'Missing', 'primary': {'text': 'The date is not stated.', 'snippet_ids': ['S1']},
        'distinctions': [{'text': 'Protocol rather than release', 'snippet_ids': ['S1']},
                         {'text': 'Version tracked separately', 'snippet_ids': ['S2']}]}]
    brief, _, coverage = structural.decode_results(json.dumps(value), PLAN, SNIPPETS, [], [],
        structural_trace={'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v6.VERSION)
    assert 'Distinctions: Protocol rather than release; Version tracked separately — The date is not stated.' in brief['answers'][0]['text']
    structural.validate_original(brief, coverage, SNIPPETS, {'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v6.VERSION)


@pytest.mark.parametrize('text', [' leading whitespace', 'trailing whitespace ', 'api_key=sk-12345678901234567890'])
def test_clause_rejects_text_changed_by_normalization_or_redaction(text):
    value = data(); value['R1']['supported'][0]['conditions'][0]['text'] = text
    with pytest.raises(ValueError):
        v6.extract(json.dumps(value), PLAN, SNIPPETS)


def test_instruction_has_primary_sentence_rule_and_component_clause_exception():
    assert "Each text is a complete plain sentence" not in v6.INSTRUCTION
    assert "Each primary is a complete plain sentence." in v6.INSTRUCTION
    assert "a component need not end with punctuation" in v6.INSTRUCTION


@pytest.mark.parametrize('length', [161, 300])
def test_supported_primary_extended_limit_and_replay(length):
    value=data(); text='A'*(length-1)+'.'; value['R1']['supported'][0]['primary']['text']=text
    brief,_,coverage=structural.decode_results(json.dumps(value),PLAN,SNIPPETS,[],[],structural_trace={'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v6.VERSION)
    assert coverage['cited_conclusions']['operations']['R1'][0]['primary_conclusion']==text
    structural.validate_original(brief,coverage,SNIPPETS,{'blocks':[]},unknown_mode='cited-unknown-v4',presentation_mode=v6.VERSION)


def test_supported_primary_301_and_other_status_161_rejected():
    value=data();value['R1']['supported'][0]['primary']['text']='A'*300+'.'
    with pytest.raises(ValueError): v6.extract(json.dumps(value),PLAN,SNIPPETS)
    for status in ('unknown','conflicting'):
        value=data();value['R1']['supported']=[]
        group={'focus':'X','primary':{'text':'A'*160+'.','snippet_ids':['S1']}}
        if status=='unknown':group['distinctions']=[]
        value['R1'][status]=[group]
        with pytest.raises(ValueError): v6.extract(json.dumps(value),PLAN,SNIPPETS)


def test_supported_composed_900_limit_exact():
    value=data();g=value['R1']['supported'][0];g['primary']['text']='A'*299+'.'
    g['conditions']=[{'text':'B'*160,'snippet_ids':['S1']},{'text':'C'*160,'snippet_ids':['S2']}]
    g['exceptions']=[{'text':'D'*160,'snippet_ids':['S1']},{'text':'E'*90,'snippet_ids':['S2']}]
    raw,_=v6.extract(json.dumps(value),PLAN,SNIPPETS)
    assert len(json.loads(raw)['R1']['groups'][0]['conclusion'])==900
    g['exceptions'][1]['text']+='E'
    with pytest.raises(ValueError, match='budget'):v6.extract(json.dumps(value),PLAN,SNIPPETS)


def test_v6_instruction_replaces_v5_limit_without_contradiction():
    from potluck_local.services import investigation_answer_components_v5 as previous
    assert v6._OLD_LIMIT in previous.INSTRUCTION and v6._OLD_LIMIT not in v6.INSTRUCTION
    assert v6._NEW_LIMIT in v6.INSTRUCTION
    assert 'supported primary is at most 300 characters' in v6.INSTRUCTION.lower()
    assert 'unknown and conflicting primaries and each component are at most 160' in v6.INSTRUCTION


@pytest.mark.parametrize('text', [
    ' leading.', 'trailing. ', 'Invalid\ncontrol.', 'Reference [s1].',
    'api_key=sk-12345678901234567890.',
])
def test_supported_primary_rejects_unsafe_or_changed_text(text):
    value = data(); value['R1']['supported'][0]['primary']['text'] = text
    with pytest.raises(ValueError):
        v6.extract(json.dumps(value), PLAN, SNIPPETS)
