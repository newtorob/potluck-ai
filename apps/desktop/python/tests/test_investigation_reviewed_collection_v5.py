import asyncio
import json
from copy import deepcopy
import pytest
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.source_collections import scan_collection
from potluck_local.services.reviewed_knowledge import delete_claim
from test_investigation_reviewed_evidence import setup_case, claim

from potluck_local.services import investigation_reviewed_collection_v5 as gate

def test_39_scope_bounded_prefix_and_conflicts(setup_case):
 async def run():
  scope,_,src=await setup_case();ids=[]
  for i in range(39): ids.append(await claim(scope,src['old.md'],f'policy.{i:02}',f'Garden irrigation policy item {i} applies.'))
  result=(await gate.prepare(scope,'garden irrigation policy'))['result'];trace=result['retrieval_trace']
  assert 'does not normalize source words' in trace['discovery_matching']
  assert len(result['reviewed_claims'])==6 and trace['omitted_count']==33 and trace['stop_reason']=='claim_count'
  assert len(trace['ranking'])==39 and len(set(trace['selected_claim_ids']+trace['omitted_claim_ids']))==39
  await delete_claim('w',scope.collection_ids,trace['selected_claim_ids'][0]);next_result=(await gate.prepare(scope,'garden irrigation policy'))['result']
  assert trace['selected_claim_ids'][0] not in [c['id'] for c in next_result['reviewed_claims']]
  with pytest.raises(ValueError):await gate.prepare(EvidenceScope('elsewhere',scope.collection_ids),'garden irrigation')
 asyncio.run(run())

def test_source_chunk_not_whole_document_fanout(setup_case):
 async def run():
  scope,folder,_=await setup_case();(folder/'old.md').write_text('# Search\nOrchard permit is mandatory and every participant must provide a complete signed permit before entering the guarded orchard facility.\n\n# Separate\nUnrelated pottery instruction explains clay preparation and drying procedures in detail for workshop participants.\n');await scan_collection('w',scope.collection_ids[0])
  from potluck_local.services.evidence_lookup import lookup_evidence
  chunks=(await lookup_evidence(scope,'orchard pottery',10,retrieval_version='v2'))['source_chunks']
  first=next(c for c in chunks if 'Orchard' in c['content']);second=next(c for c in chunks if 'pottery' in c['content'])
  ref=lambda x:{k:x[k] for k in ('collection_id','document_id','revision_id','chunk_id')}
  wanted=await claim(scope,ref(first),'orchard','Permit is mandatory.')
  other=await claim(scope,ref(second),'pottery','Clay must dry.')
  result=(await gate.prepare(scope,'orchard'))['result'];assert [c['id'] for c in result['reviewed_claims']]==[wanted]
  assert other in result['retrieval_trace']['unmatched_claim_ids']
 asyncio.run(run())

def test_conflicts_and_oversized_top_fail(setup_case,monkeypatch):
 async def run():
  scope,_,src=await setup_case();a=await claim(scope,src['old.md'],'first','Irrigation allowed with a permit.');b=await claim(scope,src['new.md'],'second','Irrigation forbidden even with a permit.')
  result=(await gate.prepare(scope,'irrigation permit'))['result']; assert {x['id'] for x in result['reviewed_claims']}=={a,b}
  monkeypatch.setattr(gate,'MAX_CLAIM_CHARS',1)
  with pytest.raises(ValueError,match='Highest ranked'): await gate.prepare(scope,'irrigation permit')
 asyncio.run(run())

def test_stale_and_superseded_authority(setup_case):
 async def run():
  from potluck_local.services.reviewed_knowledge import propose_claim, supersede_claim
  scope,folder,src=await setup_case();old=await claim(scope,src['old.md'],'orchard.rule','Orchard access requires a valid permit.')
  replacement=await propose_claim('w',scope.collection_ids,'orchard.rule','Orchard access requires two valid permits.',[src['old.md']])
  await supersede_claim('w',scope.collection_ids,old,replacement,'owner','fixture replacement')
  r=(await gate.prepare(scope,'orchard permit'))['result'];assert [x['id'] for x in r['reviewed_claims']]==[replacement]
  (folder/'old.md').write_text('# altered\nThe orchard permit text has changed.\n');await scan_collection('w',scope.collection_ids[0])
  with pytest.raises(ValueError):await gate.prepare(scope,'orchard permit')
 asyncio.run(run())

def test_selection_stops_at_first_oversize_not_skip(setup_case,monkeypatch):
 async def run():
  scope,_,src=await setup_case();await claim(scope,src['old.md'],'a','Orchard permit is needed.')
  await claim(scope,src['old.md'],'b','Orchard permit requires a substantially longer explanatory sentence.')
  await claim(scope,src['old.md'],'c','Orchard permit.')
  monkeypatch.setattr(gate,'MAX_CLAIM_CHARS',45)
  trace=(await gate.prepare(scope,'orchard permit'))['result']['retrieval_trace']
  assert trace['selected_claim_ids']==[x['claim_id'] for x in trace['ranking'][:len(trace['selected_claim_ids'])]]
  assert trace['omitted_count']>=1 and trace['stop_reason']=='claim_characters'
 asyncio.run(run())

def test_relational_terms_filtered_only_for_ranking_not_evidence_or_question(setup_case):
 async def run():
  scope,_,src=await setup_case();await claim(scope,src['old.md'],'drift','After a change, irrigation requires a permit before use without exceptions.')
  for i in range(8):await claim(scope,src['new.md'],f'noise.{i}','After a ceremony, pottery is archived.')
  query='After irrigation before use without permit';result=(await gate.prepare(scope,query))['result']
  assert result['retrieval_trace']['query_terms']['ignored_function_words']==['after','before','without']
  assert result['reviewed_claims'][0]['content']=='After a change, irrigation requires a permit before use without exceptions.'
  assert all('pottery' not in c['content'] for c in result['reviewed_claims'])
  with pytest.raises(ValueError,match='No matching'):await gate.prepare(scope,'after before without')
  from potluck_local.services import investigation_reviewed_claim_selection as render
  prepared=render.prepare(query,result['reviewed_claims']); assert query in str(render.messages(prepared))
 asyncio.run(run())

def test_inflection_matching_is_generic_and_preserves_full_text(setup_case):
 async def run():
  scope,_,src=await setup_case()
  relevant=await claim(scope,src['old.md'],'permit.rule','Inspectors requested permits before grants are issued.')
  await claim(scope,src['new.md'],'unrelated','Inspectors retain ceramic objects after inspection.')
  result=(await gate.prepare(scope,'When is a permit request granted?'))['result']
  assert relevant in [c['id'] for c in result['reviewed_claims']]
  assert next(c for c in result['reviewed_claims'] if c['id']==relevant)['content']=='Inspectors requested permits before grants are issued.'
  assert result['retrieval_trace']['query_terms']['version']=='vocabulary-suffix-expansion-v1'
 asyncio.run(run())


def test_inflection_does_not_merge_short_or_negated_tokens():
 assert gate._word_form('permits')=='permit'
 assert gate._word_form('requested')=='request'
 assert gate._word_form('grants')=='grant'
 assert gate._word_form('status')=='status'
 assert gate._word_form('not')=='not'
 assert gate._tokens('Permit_not_granted')=={'permit','not','grant'}




def test_comparatives_and_short_unicode_preserved(setup_case):
 async def run():
  scope,_,src=await setup_case()
  wanted=await claim(scope,src['old.md'],'version.rule','New device supports old device with approval.')
  result=(await gate.prepare(scope,'newer device older device'))['result']
  assert wanted in [c['id'] for c in result['reviewed_claims']]
  assert {'newer','older'} <= set(result['retrieval_trace']['query_terms']['discovery_terms'])
  assert {'new','old','newer','older'} <= set(result['retrieval_trace']['query_terms']['ranking_terms'])
  assert result['retrieval_trace']['query_terms']['suffix_expansions']=={'newer':'new','older':'old'}
  assert result['reviewed_claims'][0]['content']=='New device supports old device with approval.'
 asyncio.run(run())
 assert gate._word_form('猫')=='猫' and gate._word_form('no')=='no'

def test_concise_focused_over_long_broad(setup_case):
 async def run():
  scope,_,src=await setup_case()
  wanted=await claim(scope,src['old.md'],'focused','Hydration safety valve inspection required.')
  await claim(scope,src['new.md'],'broad','Hydration safety valve '+('unrelated narrative details different words '*45))
  result=(await gate.prepare(scope,'hydration safety valve'))['result']
  assert result['reviewed_claims'][0]['id']==wanted
 asyncio.run(run())


def test_suffix_expansion_is_non_destructive_and_ascii_only(setup_case):
 async def run():
  scope,_,src=await setup_case()
  await claim(scope,src['old.md'],'words','Water and wat occur beside the café and caféer labels.')
  result=(await gate.prepare(scope,'water caféer'))['result'];terms=result['retrieval_trace']['query_terms']
  assert 'water' in terms['ranking_terms'] and 'wat' in terms['ranking_terms']
  assert terms['suffix_expansions'].get('water')=='wat'
  assert 'caféer' in terms['ranking_terms'] and 'café' not in terms['suffix_expansions'].values()
 asyncio.run(run())


@pytest.mark.parametrize('mutation', ['revoke', 'rescan'])
def test_v5_snapshot_rejects_changed_authority_during_inference(setup_case,monkeypatch,mutation):
 from potluck_local.services import investigations as inv
 from potluck_local.services.memory_db import get_memory_db_path
 import sqlite3
 async def run():
  scope,folder,src=await setup_case()
  cid=await claim(scope,src['old.md'],'orchard.one','Orchard access requires a permit and prior inspection.')
  await claim(scope,src['new.md'],'orchard.two','Orchard access requires a second independent record.')
  class Runtime:
   calls=0
   def current_model_id(self):return 'model'
   def current_model_sha256(self):return 'a'*64
   async def chat(self,messages,**kwargs):
    Runtime.calls+=1
    payload=json.loads(messages[1]['content']);assert payload['claims'] and payload['claims'][0]['id']=='C1'
    if mutation=='revoke':await delete_claim('w',scope.collection_ids,cid)
    else:
     (folder/'old.md').write_text('# Changed\nOrchard access changed after the snapshot.\n')
     await scan_collection('w',scope.collection_ids[0])
    return {'choices':[{'message':{'content':json.dumps({'status':'supported','claim_ids':['C1']})}}]}
  monkeypatch.setattr(inv,'get_runtime',Runtime)
  with pytest.raises(ValueError,match='Reviewed evidence changed during investigation'):
   await inv.create_investigation(scope,'Orchard policy?','orchard','model',template_version='investigation-v21',reviewed_operations=[{'request':'Orchard policy?','operation':'answer'}],presentation_mode='reviewed-claim-selection-v2',evidence_selection=gate.VERSION)
  assert Runtime.calls==1
  with sqlite3.connect(get_memory_db_path()) as db:
   exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='investigations'").fetchone()
   assert not exists or db.execute('SELECT count(*) FROM investigations').fetchone()[0]==0
 asyncio.run(run())

def test_v5_unknown_notice_survives_save_and_get_when_prefix_omitted(setup_case,monkeypatch):
 import json
 from potluck_local.services import investigations as inv
 monkeypatch.setattr(gate,'MAX_SELECTED',1)
 async def run():
  scope,_,src=await setup_case()
  await claim(scope,src['old.md'],'orchard.one','Orchard inspection requires a permit.')
  await claim(scope,src['new.md'],'orchard.two','Orchard inspection requires a record.')
  class Runtime:
   def current_model_id(self):return 'model'
   def current_model_sha256(self):return 'a'*64
   async def chat(self,messages,**kwargs):return {'choices':[{'message':{'content':json.dumps({'status':'unknown','claim_ids':[]})}}]}
  monkeypatch.setattr(inv,'get_runtime',Runtime)
  result=await inv.create_investigation(scope,'Orchard unknown?','orchard','model',template_version='investigation-v21',reviewed_operations=[{'request':'Orchard unknown?','operation':'answer'}],reviewed_plan_origin='fixture_supplied',presentation_mode='reviewed-claim-selection-v1',evidence_selection=gate.VERSION)
  saved=await inv.get_investigation(scope,result['id']);notices=saved['retrieval_notices']
  assert any('1 matching claim omitted' in n and 'cannot establish whole-collection absence' in n for n in notices)
  assert saved['provenance']['retrieval_notices']==notices
 asyncio.run(run())


def test_v5_save_correct_replay(tmp_path, monkeypatch):
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
            evidence_selection=gate.VERSION, presentation_mode='reviewed-claim-selection-v2')
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
            evidence_selection=gate.VERSION, presentation_mode='reviewed-claim-selection-v2',
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



