"""Replay frozen model replies through final service code; never runs inference."""
from pathlib import Path
from types import SimpleNamespace
import asyncio,copy,hashlib,importlib.util,json,sys
w=Path(__file__).resolve().parent;r=Path('/Users/rob/Documents/potluck');b=r/'bench/results/20261030-answer-contract-repair';src=b/'fresh-v13';snap=w/'candidate-final';out=b/'post-holdout-replay'
read=lambda p:json.loads(p.read_text())
manifest=read(src/'frozen/manifest.json');calls=[]
for c in manifest['cases']:
 for d in sorted((src/'attempts'/c['id']/'raw/calls').iterdir()):calls.append((c['id'],read(d/'request.json'),read(d/'raw-reply.json') if (d/'raw-reply.json').exists() else None))
assert len(calls)==8 and (src/'raw.json').exists()
sys.path.insert(0,str(snap/'apps/desktop/python'))
from potluck_local.services import runtime as runtime_module, investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services import company_brain_eval_runner as base
from potluck_local.routers.investigations import export
spec=importlib.util.spec_from_file_location('frozen_runner',w/'run.py');runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
class ReplayRuntime:
 def __init__(self):self.index=0;self.matches=[]
 def load_model(self,*args,**kwargs):return {'mode':'recorded-response-replay','model_loaded':False,'source_run':str(src)}
 def inference_summary(self):return {'new_inference':False}
 def current_model_id(self):return 'qwen3-8b-q4'
 def current_model_sha256(self):return 'd98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785'
 async def validate_investigation_profile(self,name,**kwargs):
  from potluck_local.services.investigation_profiles import resolve_profile
  assert name=='qwen3-non-thinking' and kwargs=={'expected_model':self.current_model_id(),'expected_sha256':self.current_model_sha256()}
  metadata=resolve_profile(name).metadata();assert metadata==read(src/'run-metadata.json')['generation_profile']
  return metadata
 def unload_model(self):pass
 async def chat(self,messages,**kwargs):
  case,request,reply=calls[self.index];self.index+=1
  match=messages==request['messages'] and kwargs==request['settings']
  self.matches.append({'case':case,'call':self.index,'messages_equal':messages==request['messages'],'settings_and_schema_equal':kwargs==request['settings']})
  assert match,'Replay request changed: '+case
  if reply is None:
   self.matches[-1]['recorded_failure_without_reply']=True
   raise asyncio.TimeoutError('Recorded frozen generation had no final response; no inference attempted')
  return copy.deepcopy(reply)
from potluck_local.services import source_collections
import uuid
original_register=source_collections.register_collection
async def register_frozen_identity(workspace,name,folder,**kwargs):
 case=folder.parent.parent.name;record=read(src/'attempts'/case/'raw/registration.json')
 identifier=record['collection_id'] if name=='frozen' else record['excluded_canary']['collection_id']
 previous=source_collections.uuid
 try:
  source_collections.uuid=SimpleNamespace(uuid4=lambda:uuid.UUID(identifier))
  return await original_register(workspace,name,folder,**kwargs)
 finally:source_collections.uuid=previous
source_collections.register_collection=register_frozen_identity
engine=ReplayRuntime();runtime_module.get_runtime=lambda:engine
args=SimpleNamespace(fixture_dir=src/'frozen',fixture_sha=hashlib.sha256((src/'frozen/SHA256SUMS').read_bytes()).hexdigest(),purpose='post-holdout-decoder-replay-no-new-inference',plan_overrides=None,case_id=None,offline_check=False,backend='python',snapshot=snap/'apps/desktop/python',snapshot_manifest=snap/'snapshot.json',model=Path('/Users/rob/.potluck/models/qwen3-8b-q4/model.gguf'),sha256=engine.current_model_sha256(),output=out,version='v13',preflight_only=False)
async def main():
 await runner.run(args)
 assert engine.index==len(calls) and all(x['messages_equal'] and x['settings_and_schema_equal'] for x in engine.matches)
 checks={'new_inference':False,'meaning':'Post-holdout validation replay, not a fresh model success or human review','collection_id_policy':'Reuse frozen collection UUIDs in separate isolated DBs to preserve exact prompt-visible extraction-notice citation IDs; real scanning/retrieval otherwise unchanged','original_run':str(src),'original_metadata_sha256':hashlib.sha256((src/'run-metadata.json').read_bytes()).hexdigest(),'calls':engine.matches,'cases':{}}
 for c in manifest['cases']:
  cid=c['id'];folder=out/'attempts'/cid/'raw';before=src/'attempts'/cid/'raw';result=read(folder/'result.json');reg=read(folder/'registration.json')
  # IDs and timestamps are local to each isolated index; source order and bytes must be identical.
  def evidence_projection(d):return [(x['relative_path'],x['anchor'],x['heading'],x['content']) for x in d['result']['source_chunks']]
  assert evidence_projection(read(folder/'retrieval-001.json'))==evidence_projection(read(before/'retrieval-001.json'))
  item=result.get('result');row={'status':result['status'],'retrieved_source_order_and_content_equal':True,'captured_replies_equal':all(read(d/'raw-reply.json')==read(before/'calls'/d.name/'raw-reply.json') for d in (folder/'calls').iterdir() if (d/'raw-reply.json').exists()),'original_failure_without_reply_retained':any(not (d/'raw-reply.json').exists() for d in (before/'calls').iterdir())};checks['cases'][cid]=row
  assert row['captured_replies_equal']
  if not item:continue
  with base.isolated_environment(POTLUCK_DATA_DIR=str(out/'data'),POTLUCK_MEMORY_DB_PATH=str(folder/'isolated.sqlite')):
   scope=EvidenceScope(reg['workspace'],tuple(reg['evidence_scope_collection_ids']))
   reviewed=await inv.review_investigation(scope,item['id'],'correct','fixture-replay','Unchanged machine brief used only to test correction persistence and export; not owner approval.',corrected_brief=item['brief'])
   document=await export(item['id'],reg['workspace'],reg['evidence_scope_collection_ids'])
   row['correction_saved']=reviewed['review']['corrected_brief'] is not None;row['export_succeeded']=bool(document)
   (folder/'replay-review.json').write_text(json.dumps(reviewed,indent=2,ensure_ascii=False)+'\n');(folder/'replay-export.json').write_text(json.dumps(document,indent=2,ensure_ascii=False,default=str)+'\n')
   assert row['correction_saved'] and row['export_succeeded']
 (out/'replay-checks.json').write_text(json.dumps(checks,indent=2)+'\n')
 print(json.dumps({'calls_matched':len(engine.matches),'cases':checks['cases']}))
asyncio.run(main())
