import importlib.util,json,hashlib
from copy import deepcopy
from pathlib import Path
import pytest
from potluck_local.services import investigation_reviewed_evidence as evidence_service
from potluck_local.services import investigation_reviewed_claim_selection as v1
from potluck_local.services import investigation_reviewed_claim_selection_v2 as v2

def fixtures():
 support='\n\n'.join(f'Support paragraph {i} preserves a full unrelated detail.' for i in range(20))
 return [{'kind':'reviewed_claim','citation_id':'claim:one','content':'A complete rule.\n\nAn exception applies only after approval.', 'sources':[{'citation_id':'source:one:revision','content':support}]}]

def test_complete_claim_and_separate_bound_support_avoid_old_display_overflow():
 evidence=fixtures();snips,trace=evidence_service.snippets(evidence);assert not trace['truncated']
 raw=json.dumps({'status':'supported','claim_ids':['C1']});op={'id':'R1','request':'What is the rule?'}
 with pytest.raises(ValueError,match='display budget'):v1.make_brief(op['request'],op,evidence,snips,raw)
 brief,trace=v2.make_brief(op['request'],op,evidence,snips,raw);answer=brief['answers'][0]
 assert evidence[0]['content'] in answer['text'] and 'approval' in answer['text']
 assert all(x['citation_id']=='claim:one' for x in answer['source_excerpts'])
 assert trace['support_manifest'][0]['source_content_sha256']==hashlib.sha256(evidence[0]['sources'][0]['content'].encode()).hexdigest()
 assert len(trace['support_manifest'][0]['snippet_ids'])==20

def test_original_replay_and_support_tamper_fail():
 evidence=fixtures();snips,_=evidence_service.snippets(evidence);question='What is the rule?';op={'id':'R1','request':question};raw='{"status":"supported","claim_ids":["C1"]}'
 brief,trace=v2.make_brief(question,op,evidence,snips,raw);prepared=v2.prepare(question,evidence);visible={'model_visible_claim_ids':[c['id'] for c in prepared['claims']],'support_visibility':'Supporting source excerpts are not in the model prompt.','messages_sha256':hashlib.sha256(json.dumps(v2.messages(prepared),ensure_ascii=False,sort_keys=True).encode()).hexdigest(),'schema_sha256':hashlib.sha256(json.dumps(v2.response_schema(prepared),ensure_ascii=False,sort_keys=True).encode()).hexdigest()}
 item={'issue':question,'evidence':evidence,'brief':brief,'provenance':{'reviewed_claim_selection':trace,'reviewed_claim_selection_input':visible,'operation_plan':[op]}}
 assert v2.validate_brief(item,snips)==brief
 changed=deepcopy(item);changed['evidence'][0]['sources'][0]['content']+=' changed';new_snips,_=evidence_service.snippets(changed['evidence'])
 with pytest.raises(ValueError):v2.validate_brief(changed,new_snips)
 changed=deepcopy(item);changed['provenance']['reviewed_claim_selection']['support_manifest'][0]['source_citation_id']='source:foreign'
 with pytest.raises(ValueError):v2.validate_brief(changed,snips)

def test_bounded_claims_insufficient_unknown_and_conflicts():
 evidence=fixtures();snips,_=evidence_service.snippets(evidence);q='Policy?';op={'id':'R1','request':q}
 brief,_=v2.make_brief(q,op,evidence,snips,'{"status":"unknown","claim_ids":[]}');assert 'does not establish absence' in brief['answers'][0]['text']
 with pytest.raises(ValueError):v2.make_brief(q,op,evidence,snips,'{"status":"conflicting","claim_ids":["C1"]}')
 long=fixtures();long[0]['content']='x'*8001
 with pytest.raises(ValueError):v2.decode('{"status":"supported","claim_ids":["C1"]}',v2.prepare(q,long))
 assert 'paraphrase' in v2.INSTRUCTION and 'do not' in v2.INSTRUCTION.lower()


def test_v2_real_save_get_correct_and_original_replay(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.services import investigation_verification as verification
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def validate_investigation_profile(self, *args, **kwargs): return None
        async def measure_investigation_prompt(self, messages, **kwargs):
            Runtime.measured = (deepcopy(messages), deepcopy(kwargs['response_format']['schema']))
            return {'input_tokens': 123, 'context_tokens': 8192}
        async def chat(self, messages, **kwargs):
            Runtime.actual = (deepcopy(messages), deepcopy(kwargs['response_format']['schema']))
            assert 'claims' in json.loads(messages[1]['content'])
            return {'choices': [{'message': {'content': '{"status":"supported","claim_ids":["C1"]}'}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'; folder.mkdir()
        (folder / 'source.md').write_text('# Rule\nRollout policy includes an earlier rule.\n')
        cid = await register_collection('w', 'docs', folder); await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        result = await lookup_evidence(scope, 'rollout', 10, retrieval_version='v2')
        source = {k: result['source_chunks'][0][k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}
        claim_id = await propose_claim('w', (cid,), 'rollout', 'Rollout current rule.\n\nA second exact paragraph.', [source])
        await approve_claim('w', (cid,), claim_id, 'owner', 'reviewed')
        item = await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21',
            reviewed_operations=[{'request': 'Rollout policy?', 'operation': 'answer'}],
            evidence_selection='reviewed-current-v1', presentation_mode=v2.VERSION)
        answer = item['brief']['answers'][0]
        assert 'Rollout current rule.\n\nA second exact paragraph.' in answer['text']
        assert 'supporting sources are retained separately' in answer['text']
        assert item['evidence'][0]['sources'][0]['content']=='Rollout policy includes an earlier rule.'
        assert item['provenance']['answer_budget']['max_total_displayed_claim_passages']==16
        assert 'max_total_displayed_claim_and_support_passages' not in item['provenance']['answer_budget']
        assert item['provenance']['reviewed_claim_selection']['support_manifest']
        assert answer['snippet_ids'] and all('[' + sid + ']' in answer['text'] for sid in answer['snippet_ids'])
        retrieved = await inv.get_investigation(scope, item['id'])
        assert retrieved['brief'] == item['brief']
        with pytest.raises(ValueError, match='Automatic verification is unsupported'):
            verification.original_input(item)
        forged = deepcopy(item)
        forged['evidence'][0]['content'] = 'Changed claim.'
        with pytest.raises(ValueError, match='trace|original'):
            verification.original_input(forged)
        corrected = deepcopy(item['brief'])
        corrected['answers'][0]['text'] = 'Owner correction based on the cited sources.'
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'review', corrected_brief=corrected)
        assert reviewed['review']['corrected_brief']['answers'][0]['text'] == 'Owner correction based on the cited sources.'
        assert reviewed['brief'] == item['brief']
        measured = await inv.create_investigation(scope, 'Rollout policy?', 'rollout', 'model', template_version='investigation-v21',
            reviewed_operations=[{'request': 'Rollout policy?', 'operation': 'answer'}],
            evidence_selection='reviewed-current-v1', presentation_mode=v2.VERSION,
            generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1', reliability_mode='bounded-evidence-v1')
        budget = measured['provenance']['input_budget']
        assert budget['model_visible_claim_ids'] == ['C1']
        assert 'not in the model prompt' in budget['support_visibility']
        assert budget['input_tokens'] == 123 and budget['context_tokens'] == 8192
        assert 'selected_snippet_ids' not in budget and 'omitted_snippet_ids' not in budget
        assert Runtime.measured == Runtime.actual
        import hashlib
        digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        assert budget['messages_sha256'] == digest(Runtime.actual[0])
        assert budget['schema_sha256'] == digest(Runtime.actual[1])
        assert budget['model_visible_claim_ids'] == [c['id'] for c in json.loads(Runtime.actual[0][1]['content'])['claims']]
        corrupted = deepcopy(measured)
        corrupted['provenance']['input_budget']['messages_sha256'] = '0' * 64
        with pytest.raises(ValueError, match='budget trace'):
            verification.original_input(corrupted)
    asyncio.run(run())


