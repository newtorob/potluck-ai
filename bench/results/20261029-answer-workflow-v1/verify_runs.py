import hashlib
import json
from pathlib import Path
repo=Path(__file__).resolve().parents[3]
root=repo/'bench/results'
summary=root/'20261029-answer-workflow-v1'
freeze=json.loads((summary/'source-freeze.json').read_text())
cases=json.loads((repo/'apps/desktop/python/evals/company_brain/answer_workflow_v1/fresh.json').read_text())['cases']
report={'checks':[], 'arms':{}}
for model in ('3b','8b'):
 for seed in (7,29):
  arm=f'{model}-s{seed}'; run=root/f'20261029-answer-workflow-fresh-{arm}'
  meta=json.loads((run/'run-metadata.json').read_text()); raw=json.loads((run/'raw.json').read_text())
  assert meta['code_identity']['python_source_sha256']==freeze['python_source_sha256'],arm
  assert meta['suite_sha256']==freeze['fixtures']['fresh.json'],arm
  assert meta['rubric_sha256']==freeze['fixtures']['rubric.json'],arm
  assert meta['seed']==seed and meta['question_mode']=='auto'
  assert meta['settings']=={'n_ctx':8192,'n_threads':8,'n_gpu_layers':0,'cpu_only':True,'rpc_servers':None}
  assert raw['case_ids']==[c['id'] for c in cases]
  saved=0; citations=0; times=[]
  for case in cases:
   folder=run/'attempts'/case['id']/'raw'
   result=json.loads((folder/'result.json').read_text())
   requests=[json.loads(p.read_text()) for p in sorted(folder.glob('calls/*/request.json'))]
   assert requests
   assert json.loads(requests[0]['messages'][1]['content'])=={'issue':case['question']}
   for request in requests:
    assert request['settings']['seed']==seed and request['settings']['bounded_generation'] is True
   visible=json.dumps(requests)+ (folder/'retrieval.json').read_text()+json.dumps(result)
   assert 'CANARY-JADE-73' not in visible
   if result['status']!='success': continue
   saved+=1; item=result['result']; brief=item['brief']; prov=item['provenance']; times.append(prov['elapsed_ms'])
   assert len(requests)==2 and len(brief['questions'])==len(brief['answers'])
   payload=json.loads(requests[1]['messages'][1]['content']); supplied={s['snippet_id']:s['text'] for s in payload['snippets']}
   catalog={s['snippet_id']:s for s in prov['snippet_catalog']}
   assert {k:v['quote'] for k,v in catalog.items()}==supplied
   assert prov['generation_profile']==meta['generation_profile']
   for question, answer in zip(brief['questions'],brief['answers']):
    assert question['id']==answer['question_id']
    assert [s['snippet_id'] for s in answer['source_excerpts']]==answer['snippet_ids']
    for excerpt in answer['source_excerpts']:
     assert excerpt==catalog[excerpt['snippet_id']]
     assert excerpt['quote'] in case['allowed_source']
     citations+=1
  report['arms'][arm]={'attempts':len(cases),'saved':saved,'exact_excerpts_checked':citations,'elapsed_ms':times}
report['checks']=['All four arms match frozen production Python source hashes, fixtures, rubric and CPU settings','Original issue only in every planner call; both model calls retained','Seed and bounded generation recorded on each call','Selected snippets exactly match presented source text and saved catalog','No excluded canary in requests, retrieval or answers']
(summary/'integrity.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:{a:b for a,b in v.items() if a!='elapsed_ms'} for k,v in report['arms'].items()},indent=2))
