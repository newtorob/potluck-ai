import asyncio
import pytest
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.source_collections import scan_collection
from potluck_local.services.reviewed_knowledge import delete_claim
from test_investigation_reviewed_evidence import setup_case, claim

from potluck_local.services import investigation_reviewed_collection_v4 as gate

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
  assert len(trace['selected_claim_ids'])==1 and trace['omitted_count']==2 and trace['stop_reason']=='claim_characters'
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
  assert result['retrieval_trace']['query_terms']['version']=='inflection-filter-v1'
 asyncio.run(run())


def test_inflection_does_not_merge_short_or_negated_tokens():
 assert gate._word_form('permits')=='permit'
 assert gate._word_form('requested')=='request'
 assert gate._word_form('grants')=='grant'
 assert gate._word_form('status')=='status'
 assert gate._word_form('not')=='not'
 assert gate._tokens('Permit_not_granted')=={'permit','not','grant'}


def test_v4_snapshot_rejects_revocation_during_inference(setup_case,monkeypatch):
 import json
 from potluck_local.services import investigations as inv
 async def run():
  scope,_,src=await setup_case();cid=await claim(scope,src['old.md'],'orchard','Orchard access requires a permit and prior inspection.')
  class Runtime:
   def current_model_id(self):return 'model'
   def current_model_sha256(self):return 'a'*64
   async def chat(self,messages,**kwargs):
    payload=json.loads(messages[1]['content']);await delete_claim('w',scope.collection_ids,cid)
    return {'choices':[{'message':{'content':json.dumps({'R1':{'groups':[{'focus':'Orchard','snippet_ids':[payload['snippets'][0]['snippet_id']],'status':'supported'}],'more':False}})}}]}
  monkeypatch.setattr(inv,'get_runtime',Runtime)
  with pytest.raises(ValueError):await inv.create_investigation(scope,'Orchard policy?','orchard','model',template_version='investigation-v21',reviewed_operations=[{'request':'Orchard policy?','operation':'answer'}],evidence_selection=gate.VERSION)
 asyncio.run(run())

def test_v4_unknown_notice_survives_save_and_get_when_prefix_omitted(setup_case,monkeypatch):
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
