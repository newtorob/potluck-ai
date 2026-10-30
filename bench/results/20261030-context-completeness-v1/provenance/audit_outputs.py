#!/usr/bin/env python3
"""Read-only artifact audit. No inference or semantic assessment."""
import argparse, hashlib, json, re, sys
from pathlib import Path

def read(p): return json.loads(p.read_text())
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def canon(x): return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def strings(x):
 if isinstance(x,str): return [x]
 if isinstance(x,dict): return sum((strings(v) for v in x.values()),[])
 if isinstance(x,list): return sum((strings(v) for v in x),[])
 return []
def absent(obj,text,lines): return all(text not in s and all(line not in s for line in lines) for s in strings(obj))
def identity_matches(metadata, snapshot):
 prefix='apps/desktop/python/'
 expected={name[len(prefix):]:digest for name,digest in snapshot['production_hashes'].items() if name.startswith(prefix+'potluck_local/') and name.endswith('.py')}
 recorded=metadata.get('code_identity',{}).get('python_source_sha256',{})
 return {'matches':recorded==expected,'expected_count':len(expected),'recorded_count':len(recorded),'missing':sorted(set(expected)-set(recorded)),'extra':sorted(set(recorded)-set(expected)),'changed':sorted(k for k in expected.keys() & recorded.keys() if expected[k]!=recorded[k])}
def frozen(root):
 d=root/'frozen'; entries={}
 assert sha(d/'SHA256SUMS')=='f80904200972dd990d0502b7447833b6f3e18eb269390a1afdebbb2113c71afd'
 for line in (d/'SHA256SUMS').read_text().splitlines():
  h,n=line.split('  ',1); p=(d/n).resolve(strict=True)
  assert p.is_relative_to(d.resolve()) and n not in entries and sha(p)==h,n
  entries[n]=h
 meta=read(d/'sources.json')
 for s in meta['sources']: assert entries.get(s['file'])==s['sha256'],s['file']
 return entries,read(d/'manifest.json'),meta

def audit(root, case, hashes, meta, calc, answers, context, version, consumed=False):
 folder=root/'attempts'/case['id']/'raw'; errors=[]; checks={}; retrieval=[]; payloads=[]
 def ck(name,good):
  checks[name]=bool(good)
  if not good: errors.append(name)
 regpath=folder/'registration.json'; reg=read(regpath) if regpath.exists() else {}
 expected={(Path(n).name if consumed else n):hashes[n] for n in case['sources']}; actual={}
 for n in expected:
  p=folder/'allowed'/n
  if p.is_file(): actual[n]=sha(p)
 ck('registered_allowed',reg.get('files')==expected and actual==expected)
 ex=reg.get('excluded_canary',{}); text=''; canary_lines=[]
 if not consumed:
  canary=meta['excluded_canary']; cp=folder/'excluded'/canary
  ck('registered_excluded',ex.get('file')==canary and ex.get('sha256')==hashes[canary] and cp.is_file() and sha(cp)==hashes[canary])
  ck('scope_excludes_canary',bool(reg.get('collection_id')) and reg.get('evidence_scope_collection_ids')==[reg.get('collection_id')] and ex.get('collection_id')!=reg.get('collection_id'))
  text=(root/'frozen'/canary).read_text(); canary_lines=[x.strip() for x in text.splitlines() if len(x.strip()) >= 30]
 terms=re.findall(r'\w+',case['question'].lower()); terms=list(dict.fromkeys(t for t in terms if len(t)>3))
 for p in sorted(folder.glob('retrieval-*.json')):
  obj=read(p); result=obj.get('result'); serialized=canon(result)
  ck('retrieval_scope_'+p.name,obj.get('workspace')==reg.get('workspace') and obj.get('collection_ids')==([reg.get('collection_id')] if consumed else reg.get('evidence_scope_collection_ids')))
  if not consumed: ck('retrieval_canary_absent_'+p.name,absent(result,text,canary_lines))
  retrieval.append({'file':p.name,'query':obj.get('query'),'result':result,'query_term_coverage':{t:t in serialized.lower() for t in terms},'error':obj.get('error')})
 for p in sorted((folder/'calls').glob('*/request.json')):
  obj=read(p); messages=obj.get('messages',[])
  if not consumed: ck('prompt_canary_absent_'+p.parent.name,absent(messages,text,canary_lines))
  reply=p.parent/'raw-reply.json'
  if not consumed:
   ck('raw_reply_canary_absent_'+p.parent.name,reply.is_file() and absent(read(reply),text,canary_lines))
  for m in messages:
   if m.get('role')=='user':
    try: payloads.append(json.loads(m['content']))
    except (ValueError,TypeError): payloads.append(m.get('content'))
 result_path=folder/'result.json'; data=read(result_path) if result_path.exists() else {'status':'missing_result'}
 item=data.get('result'); report={'status':data.get('status'),'error':data.get('error'),'checks':checks,'errors':errors,'retrieval':retrieval,'payloads':payloads}
 if not item: return report
 if not consumed: ck('answer_canary_absent',absent(item.get('brief',{}),text,canary_lines))
 prov=item.get('provenance',{}); snippets=prov.get('snippet_catalog',[]); evidence=item.get('evidence',[]); by_id={s['snippet_id']:s for s in snippets}; contexts={e['citation_id']:e.get('presented_content','') for e in evidence}
 for i,e in enumerate(evidence):
  name=e.get('relative_path',''); source=folder/'allowed'/name
  ck('attached_source_'+str(i),name in expected and e.get('collection_id')==reg.get('collection_id') and source.is_file() and bool(e.get('content')) and e.get('content','') in source.read_text() and bool(e.get('presented_content')) and e.get('presented_content','') in e.get('content',''))
 ck('all_snippets_in_presented_content',all(s.get('quote') and s['quote'] in contexts.get(s.get('citation_id'),'') for s in snippets))
 if version=='v11':
  rebuilt,trace=context.snippets_from_evidence(evidence); ck('paragraph_trace_rebuilt',trace==prov.get('snippet_trace')); report['paragraph_trace']=trace
 else: rebuilt=answers.snippets_from_evidence(evidence)
 ck('snippets_rebuilt',rebuilt==snippets)
 source_context={e['citation_id']:e['content'] for e in evidence}
 catalog=calc.quantity_catalog(snippets,source_context=source_context,version='v3'); notices=calc.extraction_notices(snippets,catalog,source_context=source_context,version='v3')
 ck('v3_catalog_and_notices_rebuilt',prov.get('quantity_parser_version')=='v3' and catalog==prov.get('quantity_catalog') and notices==prov.get('extraction_notices'))
 for i,t in enumerate(prov.get('calculation_traces',[])):
  if t.get('status')=='computed':
   rebuilt=calc.calculate(t['operation'],[q['id'] for q in t['inputs']],catalog,version='v3'); ck('calculation_replay_'+str(i),all(t.get(k)==v for k,v in rebuilt.items()))
 spans={}
 for i,a in enumerate(item.get('brief',{}).get('answers',[])):
  def binding(x):
   ids=x.get('snippet_ids',[]); return all(s in by_id for s in ids) and x.get('source_excerpts',[])==[by_id[s] for s in ids]
  ck('answer_binding_'+str(i),binding(a))
  parts=a.get('parts',[]); cursor=0; offsets=[]; gaps=[]
  for j,p in enumerate(parts):
   ck('part_binding_'+str(i)+'_'+str(j),binding(p)); span=p.get('request_span',''); start=case['question'].find(span,cursor) if span else -1
   ck('part_ordered_span_'+str(i)+'_'+str(j),start>=cursor)
   if start>=cursor: gaps.append(case['question'][cursor:start]); offsets.append([start,start+len(span)]); cursor=start+len(span)
  if parts:
   gaps.append(case['question'][cursor:]); spans[a.get('question_id')]={'offsets':offsets,'gaps':gaps}
   ids=list(dict.fromkeys(s for p in parts for s in p.get('snippet_ids',[]))); status='conflicting' if any(p['status']=='conflicting' for p in parts) else 'unknown' if any(p['status']=='unknown' for p in parts) else 'supported'
   ck('aggregate_derived_'+str(i),a.get('text')=='\n'.join(p['text'] for p in parts) and a.get('snippet_ids')==ids and a.get('status')==status)
 if version=='v11' and spans:
  ck('coverage_gaps_rebuilt',all(prov.get('answer_coverage',{}).get('unmatched_request_text',{}).get(k)==v['gaps'] for k,v in spans.items()))
 report.update(snippet_count=len(snippets),evidence_count=len(evidence),spans=spans,missing_evidence=item.get('missing_evidence'),evidence=[{'path':e.get('relative_path'),'presented_content':e.get('presented_content'),'citation_id':e.get('citation_id')} for e in evidence],brief=item.get('brief'))
 return report

def main():
 p=argparse.ArgumentParser(); p.add_argument('--arm-a',type=Path,required=True); p.add_argument('--arm-b',type=Path,required=True); p.add_argument('--snapshot',type=Path,required=True); p.add_argument('--contract',type=Path,required=True); p.add_argument('--output',type=Path,required=True); a=p.parse_args()
 roots=[a.arm_a,a.arm_b]; assert all((r/'raw.json').is_file() for r in roots),'both raw.json files required'
 frozen_data=[frozen(r) for r in roots]; assert frozen_data[0]==frozen_data[1], 'frozen inputs differ'
 hashes,manifest,meta=frozen_data[0]; metas=[read(r/'run-metadata.json') for r in roots]; contract=read(a.contract)
 assert metas[0]['model_sha256']==metas[1]['model_sha256']
 sys.path.insert(0,str(a.snapshot)); from potluck_local.services import investigation_calculations as calc, investigation_answers as answers, investigation_context as context
 report={'semantic_assessment':False,'notes':['Exact bindings and substring checks do not establish semantic support.','All cases, including errors, remain in the denominator.'],'cases':{},'model_sha256':metas[0]['model_sha256'],'frozen_checksums':hashes,'snapshot_checks':[]}
 report['metadata_checks']={'generation_profile_equal':metas[0].get('generation_profile')==metas[1].get('generation_profile'),'settings_equal':metas[0].get('settings')==metas[1].get('settings'),'seed_equal_expected':all(m.get('seed')==contract['seed'] for m in metas),'retries_equal_expected':all(m.get('retries')==contract['retries'] for m in metas),'model_sha_expected':all(m.get('model_sha256')==contract['model_sha256'] for m in metas),'template_versions':all(m.get('template_version')==v for m,v in zip(metas,['investigation-v10','investigation-v11'])),'plan_origin':all(m.get('plan_origin')=='fixture_supplied' for m in metas),'fixture_hashes':all(m.get('fixture_sha256s')==hashes for m in metas),'no_assessment':all(m.get('assessment') is None for m in metas)}
 report['code_identity_checks']=[identity_matches(m,contract['snapshots'][name]) for m,name in zip(metas,['baseline-frozen','candidate-frozen'])]
 # Compare full contract hash inventories and every frozen snapshot file, not live production.
 for m,snapshot_name in zip(metas,['baseline-frozen','candidate-frozen']):
  sm=Path(m['snapshot_validation']['manifest']); saved=read(sm); candidates=list(contract['snapshots'].values())
  matching=[v for v in candidates if v['production_hashes']==saved['production_hashes']]
  matches=saved['production_hashes']==contract['snapshots'][snapshot_name]['production_hashes'] and bool(matching) and sha(sm)==m['snapshot_validation']['manifest_sha256']
  failures=[n for n,h in saved['production_hashes'].items() if not (sm.parent/n).is_file() or sha(sm.parent/n)!=h]
  report['snapshot_checks'].append({'matches_contract':matches,'files_mismatched':failures,'file_count':len(saved['production_hashes'])})
 raws=[read(r/'raw.json') for r in roots]
 for c in manifest['cases']:
  arms=[]
  for i,r in enumerate(roots):
   try: x=audit(r,c,hashes,meta,calc,answers,context,['v10','v11'][i])
   except Exception as e: x={'status':'audit_error','errors':[type(e).__name__+': '+str(e)]}
   if raws[i].get('outputs',{}).get(c['id']) != (read(r/'attempts'/c['id']/'raw'/'result.json') if (r/'attempts'/c['id']/'raw'/'result.json').exists() else None): x.setdefault('errors',[]).append('raw_result_mismatch')
   arms.append(x)
  normalized=[]
  for x in arms:
   normalized.append(sorted([{'path':e['path'],'presented_content':e['presented_content']} for e in x.get('evidence',[])],key=canon))
  report['cases'][c['id']]={'arms':arms,'presented_content_normalized_equal':normalized[0]==normalized[1] if all('evidence' in x for x in arms) else None,'normalized_evidence':normalized}
 a.output.mkdir(exist_ok=False,parents=True)
 with (a.output/'integrity.json').open('x') as f: json.dump(report,f,indent=2,ensure_ascii=False); f.write('\n')
if __name__=='__main__': main()
