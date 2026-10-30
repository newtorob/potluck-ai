#!/usr/bin/env python3
"""Read-only integrity checks for v13 attempts; not a semantic grader."""
import argparse, hashlib, json, sys
from pathlib import Path

def read(p): return json.loads(p.read_text())
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def strings(x):
 if isinstance(x,str): yield x
 elif isinstance(x,dict):
  for v in x.values(): yield from strings(v)
 elif isinstance(x,list):
  for v in x: yield from strings(v)

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--run',type=Path,required=True); ap.add_argument('--snapshot',type=Path,required=True); a=ap.parse_args()
 run=a.run.resolve(); snapshot=a.snapshot.resolve(); root=snapshot if (snapshot/'snapshot.json').exists() else snapshot.parents[2]; manifest_path=root/'snapshot.json'; metadata=read(run/'run-metadata.json'); sm=read(manifest_path); pyroot=root/'apps/desktop/python'; sys.path.insert(0,str(pyroot))
 from potluck_local.services import investigation_context as ctx, investigation_calculations as calc, investigation_bounded_answers as bounded
 meta=metadata.get('snapshot_validation',{}); inventory=sm['production_hashes']; recorded=metadata['code_identity']['python_source_sha256']; prefix='apps/desktop/python/'; expected={k[len(prefix):]:v for k,v in inventory.items() if k.startswith(prefix+'potluck_local/') and k.endswith('.py')}
 report={'semantic_assessment':False,'snapshot':{'manifest_hash_matches':sha(manifest_path)==meta.get('manifest_sha256'),'code_identity_matches':expected==recorded,'mismatched_files':[k for k,v in inventory.items() if not (root/k).is_file() or sha(root/k)!=v]},'fixture':{},'cases':{}}
 frozen=run/'frozen'; sums={}
 for line in (frozen/'SHA256SUMS').read_text().splitlines():
  h,name=line.split('  ',1); file=(frozen/name).resolve(); sums[name]=h
  if not file.is_relative_to(frozen.resolve()) or not file.is_file() or sha(file)!=h: report['fixture'][name]=False
 source_meta=read(frozen/'sources.json'); canary=source_meta['excluded_canary']; canary=canary['file'] if isinstance(canary,dict) else canary; canary_text=(frozen/canary).read_text(); canary_lines=[x.strip() for x in canary_text.splitlines() if len(x.strip())>=30]
 report['fixture']['source_hashes_match']=all(sums.get(x['file'])==x['sha256'] and sha(frozen/x['file'])==x['sha256'] for x in source_meta['sources']); report['fixture']['checksum_inventory_sha256']=sha(frozen/'SHA256SUMS')
 for case in read(frozen/'manifest.json')['cases']:
  folder=run/'attempts'/case['id']/'raw'; result=folder/'result.json'; checks={}; errors=[]
  def ck(name,condition):
   checks[name]=bool(condition)
   if not condition: errors.append(name)
  entry={'status':'pending','checks':checks,'errors':errors}; report['cases'][case['id']]=entry
  if not folder.exists(): continue
  if not (folder/'registration.json').exists(): continue
  reg=read(folder/'registration.json'); wanted={n:sums[n] for n in case['sources']}; ck('registered_allowed',reg.get('files')==wanted and all((folder/'allowed'/n).is_file() and sha(folder/'allowed'/n)==h for n,h in wanted.items()))
  ex=reg.get('excluded_canary',{}); ck('canary_excluded_scope',reg.get('evidence_scope_collection_ids')==[reg.get('collection_id')] and ex.get('collection_id')!=reg.get('collection_id') and ex.get('file')==canary and ex.get('sha256')==sums[canary] and (folder/'excluded'/canary).is_file() and sha(folder/'excluded'/canary)==sums[canary])
  for f in [*folder.glob('retrieval-*.json'),*folder.glob('calls/*/request.json'),*folder.glob('calls/*/raw-reply.json')]:
   obj=read(f); ck('canary_absent_'+str(f.relative_to(folder)),all(canary_text not in s and all(t not in s for t in canary_lines) for s in strings(obj)))
   if f.name.startswith('retrieval-'): ck('retrieval_scope_'+f.name,obj.get('collection_ids')==[reg.get('collection_id')] and obj.get('workspace')==reg.get('workspace'))
  if not result.exists(): continue
  d=read(result); entry['status']=d.get('status'); entry['error']=d.get('error'); item=d.get('result')
  if not item: continue
  ck('canary_absent_brief',all(canary_text not in s and all(t not in s for t in canary_lines) for s in strings(item['brief'])))
  prov=item['provenance']; evidence=item['evidence']; snippets=prov['snippet_catalog']; byid={s['snippet_id']:s for s in snippets}; contexts={e['citation_id']:e['content'] for e in evidence}; presented={e['citation_id']:e.get('presented_content','') for e in evidence}
  for j,e in enumerate(evidence):
   n=e['relative_path']; ck('exact_source_'+str(j),n in wanted and e['collection_id']==reg.get('collection_id') and bool(e['content']) and e['content'] in (folder/'allowed'/n).read_text() and e['content'].startswith(e.get('presented_content','')))
  rebuilt,trace=ctx.snippets_from_evidence(evidence,version='paragraph-v2'); ck('snippet_replay',rebuilt==snippets and trace==prov.get('snippet_trace'))
  ck('snippet_prefix',all(not content.strip() or content.startswith(''.join(s['quote'] for s in snippets if s['citation_id']==cid)) for cid,content in presented.items()))
  try:
   q=calc.quantity_catalog(snippets,source_context=contexts,version='v4'); notice=calc.extraction_notices(snippets,q,source_context=contexts,version='v4'); ck('v4_catalog_replay',q==prov.get('quantity_catalog') and notice==prov.get('extraction_notices') and prov.get('quantity_parser_version')=='v4')
   for j,t in enumerate(prov.get('calculation_traces',[])):
    if t.get('status')=='computed':
     rr=calc.calculate(t['operation'],[x['id'] for x in t['inputs']],q,version='v4'); ck('calculation_replay_'+str(j),all(t.get(k)==v for k,v in rr.items()))
  except Exception as exc: errors.append('catalog exception: '+repr(exc))
  if prov.get('answer_part_planner_version') == 'request-spans-fallback-v1':
   plan=prov.get('operation_plan',[]); factual=[row for row in plan if row['operation']=='answer']; fixed=prov.get('answer_part_plan',{}); validation=prov.get('answer_part_plan_validation',{}); calls=sorted((folder/'calls').glob('*/raw-reply.json'))
   try:
    if factual:
     proposed=read(calls[0])['choices'][0]['message']['content']; replayed,replay_trace=bounded.decode_plan(proposed,plan)
     ck('planner_replay',replayed==fixed and replay_trace==validation)
    else: ck('planner_absent',not fixed and not validation)
    ck('planner_version',prov.get('answer_part_planner_version')=='request-spans-fallback-v1')
    for row in factual:
     rid=row['id']; req=row['request']; cursor=0; selected=fixed[rid]; trace=validation[rid]
     for part in selected:
      span=part['request_span']; start=req.find(span,cursor); ck('exact_span_'+part['id'],start>=cursor); cursor=start+len(span)
     if trace['mode']=='original_request_fallback': ck('fallback_original_'+rid,len(selected)==1 and selected[0]['request_span']==req)
     ck('part_budget_'+rid,sum(bounded.part_text_limits({rid:selected}).values())+len(selected)-1<=4000)
   except Exception as exc: errors.append('planner exception: '+repr(exc))
  def binding(x):
   ids=x.get('snippet_ids',[]); return all(i in byid for i in ids) and x.get('source_excerpts',[])==[byid[i] for i in ids]
  for j,answer in enumerate(item['brief']['answers']):
   ck('answer_binding_'+str(j),binding(answer))
   for k,part in enumerate(answer.get('parts',[])): ck('part_binding_'+str(j)+'_'+str(k),binding(part))
   if prov.get('answer_part_planner_version') == 'request-spans-fallback-v1' and answer.get('parts'):
    parts=answer['parts']; rid=answer['question_id']; planned=prov['answer_part_plan'][rid]
    ck('visible_spans_'+str(j),[(p.get('id'),p.get('request_span')) for p in parts]==[(p['id'],p['request_span']) for p in planned] and all('label' not in p for p in parts))
    ck('aggregate_budget_'+str(j),len(answer['text'])<=4000 and answer['text']=='\n'.join(p['text'] for p in parts) and all(len(p['text'])<=bounded._budget(len(parts)) for p in parts))
 print(json.dumps(report,indent=2,ensure_ascii=False))
if __name__=='__main__': main()
