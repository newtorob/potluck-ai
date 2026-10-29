import copy, hashlib, json, os, pathlib, sys
work=pathlib.Path(__file__).resolve().parent
repo=pathlib.Path('/Users/rob/Documents/potluck')
engine=work/'candidate-frozen/apps/desktop/python'
sys.path.insert(0,str(engine))
root=repo/'bench/results/20261029-source-review-v1'
arm=root/'v9-fixture_supplied-s7'
os.environ['POTLUCK_DATA_DIR']=str(arm/'data')
os.environ.pop('POTLUCK_SIDECAR_TOKEN',None)
from fastapi.testclient import TestClient
from potluck_local.app import create_app
from potluck_local.services import investigation_calculations as calc, investigations as inv
from potluck_local.services.company_brain_eval_runner import isolated_environment
raw=json.loads((arm/'raw.json').read_text())
fixture=json.loads((arm/'fresh.json').read_text())
rubric=json.loads((arm/'rubric.json').read_text())
snapshot=json.loads((work/'candidate-frozen/snapshot.json').read_text())
assert json.loads((arm/'run-metadata.json').read_text())['code_identity']['python_source_sha256']==snapshot['python_source_sha256']
report={'attempts':len(raw['outputs']),'saved_drafts':0,'exact_excerpts':0,'catalogs_rebuilt':0,'computed_traces_replayed':0,'scripted_reviews':{},'actual_owner_acceptance':False,'model_calls_during_review':0}
client=TestClient(create_app(),client=('127.0.0.1',45123))
def forbidden_runtime():
 report['model_calls_during_review']+=1
 raise AssertionError('Review must not invoke model')
inv.get_runtime=forbidden_runtime
for case in fixture['cases']:
 caseid=case['id'];attempt=raw['outputs'][caseid]
 if attempt['status']!='success':
  report['scripted_reviews'][caseid]={'status':'not_run','reason':'No saved draft'}
  continue
 report['saved_drafts']+=1
 item=attempt['result'];p=item['provenance'];snippets=p['snippet_catalog'];by_sid={s['snippet_id']:s for s in snippets}
 assert all(s['quote'] in case['allowed_source'] for s in snippets)
 for a in item['brief']['answers']:
  assert a['source_excerpts']==[by_sid[s] for s in a['snippet_ids']]
  report['exact_excerpts']+=len(a['source_excerpts'])
 context={e['citation_id']:e['content'] for e in item['evidence']}
 version=p.get('quantity_parser_version','v2')
 catalog=calc.quantity_catalog(snippets,source_context=context,version=version)
 assert catalog==p['quantity_catalog']
 assert calc.extraction_notices(snippets,catalog,source_context=context,version=version)==p['extraction_notices']
 report['catalogs_rebuilt']+=1
 for trace in p['calculation_traces']:
  if trace['status']=='computed':
   rebuilt=calc.calculate(trace['operation'],[q['id'] for q in trace['inputs']],catalog,version=version)
   assert all(trace[k]==v for k,v in rebuilt.items())
   report['computed_traces_replayed']+=1
 requirement=rubric['cases'][caseid]['machine']
 canary=requirement.get('forbidden_canary')
 if canary:
  for name in ('request.json','raw-reply.json','retrieval.json','result.json'):
   assert canary not in (arm/'attempts'/caseid/'raw'/name).read_text()
 required=requirement['required_quotes']
 sids=[s['snippet_id'] for s in snippets if any(q in s['quote'] for q in required)]
 assert all(any(q in by_sid[s]['quote'] for s in sids) for q in required)
 def quantity(value,unit=None):
  hits=[q['id'] for q in catalog if q['value']==value and (unit is None or q['unit']==unit)]
  assert len(hits)==1,(value,hits)
  return hits[0]
 if caseid=='zero-baseline':
  status='calculate';ids=[quantity('0'),quantity('6')];reason='Fixture-script selection: Monday zero support tickets and Tuesday six. Relative change has a zero baseline.'
 elif caseid=='conflicting-capacity':
  status='conflicting';ids=[];reason='Fixture-script assessment: prior 30 workshop seats; current sources disagree at 42 and 48. Neither is designated authoritative, so the change is unresolved.'
 elif caseid=='incomplete-share':
  status='missing';ids=[quantity('12')];reason='Fixture-script assessment: 12 volunteers completed orientation; enrolled total has not been recorded, so the denominator is missing.'
 else:
  status='calculate';ids=[quantity('24','customer visits'),quantity('37','customer visits')];reason='Fixture-script selection: subtract south 24 customer visits from north 37; five meeting rooms is unrelated.'
 rows=[{'request_id':'R1','status':status,'operand_ids':ids,'snippet_ids':sids,'reason':reason}]
 scope={'workspace':item['workspace'],'collection_ids':item['collection_ids']}
 folder=arm/'attempts'/caseid/'raw';target=root/'scripted-review'/caseid;target.mkdir(parents=True,exist_ok=False)
 with isolated_environment(POTLUCK_MEMORY_DB_PATH=str(folder/'isolated.sqlite')):
  url='/company-brain/investigations/'+item['id']
  r=client.post(url+'/calculation-preview',json={**scope,'calculation_reviews':rows});assert r.status_code==200,r.text
  preview=r.json()
  correction=copy.deepcopy(item['brief']);correction['answers']=preview['answers']
  payload={**scope,'decision':'correct','reviewer':'fixture-script (not owner acceptance)','reason':'Scripted qualification only; not actual owner review.','corrected_brief':correction,'calculation_reviews':rows}
  r=client.post(url+'/review',json=payload);assert r.status_code==200,r.text
  saved=r.json();assert saved['brief']==item['brief'] and saved['provenance']==p
  assert saved['review']['corrected_brief']==correction
  exported=client.get(url+'/export',params=scope);assert exported.status_code==200,exported.text
  for name,obj in [('submitted.json',payload),('preview.json',preview),('saved-review.json',saved)]:
   (target/name).write_text(json.dumps(obj,indent=2)+'\n')
  (target/'handoff.md').write_text(exported.json()['markdown'])
 trace=preview['answers'][0]['calculation']
 if caseid=='zero-baseline':assert trace['reason']=='The baseline is zero.' and trace['result'] is None
 if caseid=='compound-count':assert trace['result']=='13' and trace['unit']=='customer visits'
 report['scripted_reviews'][caseid]={'status':'verified','choice':status,'answer_status':preview['answers'][0]['status'],'result':trace['result'],'cited_excerpts':len(sids),'original_preserved':True}
assert report['model_calls_during_review']==0
(root/'artifact-verification.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
