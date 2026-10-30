#!/usr/bin/env python3
"""Read-only consumed real-source development audit for v12 dev2."""
import argparse,json,sys
from pathlib import Path
from audit_outputs import audit,read,sha,identity_matches
from audit_development import load

def main():
 p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 assert (a.run/'raw.json').is_file(),'completed raw.json required'
 hashes,meta,manifest=load(a.run,'2663630bd7e60f84f98ee879ac2b4069b566347a3b55937d63530d9aca786372');ids={'pilot-readiness','core-company'};cases=[c for c in manifest['cases'] if c['id'] in ids];assert {c['id'] for c in cases}==ids
 sys.path.insert(0,str(a.snapshot));from potluck_local.services import investigation_calculations as calc, investigation_answers as answers, investigation_context as context, investigation_planned_answers as planned, investigation_reviewed_operations as reviewed
 sm=a.snapshot.parent.parent.parent/'snapshot.json';saved=read(sm);root=sm.parent;m=read(a.run/'run-metadata.json');raw=read(a.run/'raw.json')
 failures=[n for n,h in saved['production_hashes'].items() if not (root/n).is_file() or sha(root/n)!=h]
 checks={'recorded_fixture_hashes':m.get('fixture_sha256s')==hashes,'recorded_snapshot_manifest':m.get('snapshot_validation',{}).get('manifest_sha256')==sha(sm),'snapshot_path':Path(m.get('snapshot','')).resolve()==a.snapshot.resolve(),'version':m.get('template_version')=='investigation-v12','seed':m.get('seed')==7,'retries':m.get('retries')==0,'plan_origin':m.get('plan_origin')=='fixture_supplied','raw_case_ids':raw.get('case_ids')==[c['id'] for c in cases],'no_assessment':m.get('assessment') is None and raw.get('assessment') is None}
 out={'purpose':'consumed development, not fresh holdout','semantic_assessment':False,'notes':['Exact bindings do not establish semantic support.','All attempts remain in denominator, including failures.'],'fixture_sums_sha256':sha(a.run/'frozen'/'SHA256SUMS'),'snapshot_manifest_sha256':sha(sm),'snapshot_files_mismatched':failures,'metadata_checks':checks,'code_identity':identity_matches(m,saved),'cases':{}}
 for c in cases:
  try:x=audit(a.run,c,hashes,meta,calc,answers,context,planned,reviewed,'v12',consumed=True,factual_without_calculations=True)
  except Exception as e:x={'status':'audit_error','errors':[type(e).__name__+': '+str(e)]}
  result=a.run/'attempts'/c['id']/'raw'/'result.json';x.setdefault('errors',[])
  if raw.get('outputs',{}).get(c['id'])!=(read(result) if result.is_file() else None):x['errors'].append('raw_result_mismatch')
  out['cases'][c['id']]=x
 a.output.mkdir(parents=True,exist_ok=False)
 with (a.output/'integrity.json').open('x') as f:json.dump(out,f,indent=2,ensure_ascii=False);f.write('\n')
if __name__=='__main__':main()
