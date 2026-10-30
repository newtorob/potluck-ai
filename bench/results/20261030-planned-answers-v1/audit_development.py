#!/usr/bin/env python3
"""Read-only mechanical audit for the two consumed development runs."""
import argparse, json, sys
from pathlib import Path
from audit_outputs import audit, read, sha, identity_matches

def load(root, expected):
 d=root/'frozen'; assert sha(d/'SHA256SUMS')==expected, 'fixture checksum manifest differs'
 hashes={}
 for line in (d/'SHA256SUMS').read_text().splitlines():
  h,n=line.split('  ',1); file=(d/n).resolve(strict=True)
  assert file.is_relative_to(d.resolve()) and n not in hashes and sha(file)==h,n
  hashes[n]=h
 meta=read(d/'sources.json')
 for source in meta['sources']: assert hashes.get(source['file'])==source['sha256'],source['file']
 return hashes,meta,read(d/'manifest.json')

def main():
 p=argparse.ArgumentParser(); p.add_argument('--real',type=Path,required=True); p.add_argument('--conflict',type=Path,required=True); p.add_argument('--snapshot',type=Path,required=True); p.add_argument('--output',type=Path,required=True); a=p.parse_args()
 roots=[a.real,a.conflict]; assert all((r/'raw.json').is_file() for r in roots),'both development runs must be complete'
 sys.path.insert(0,str(a.snapshot)); from potluck_local.services import investigation_calculations as calc, investigation_answers as answers, investigation_context as context, investigation_planned_answers as planned, investigation_reviewed_operations as reviewed
 sm=a.snapshot.parent.parent.parent/'snapshot.json'; saved=read(sm); root=sm.parent
 mismatched=[n for n,h in saved['production_hashes'].items() if not (root/n).is_file() or sha(root/n)!=h]
 report={'purpose':'consumed development only; no fresh holdout or controlled comparison','semantic_assessment':False,'notes':['Exact source and citation bindings do not establish semantic support.','All declared attempts including errors remain in the denominator.'],'snapshot_manifest_sha256':sha(sm),'snapshot_files_mismatched':mismatched,'runs':{}}
 for run,expected,ids,flat in [(a.real,'2663630bd7e60f84f98ee879ac2b4069b566347a3b55937d63530d9aca786372',{'pilot-readiness','core-company'},True),(a.conflict,'f80904200972dd990d0502b7447833b6f3e18eb269390a1afdebbb2113c71afd',{'quill'},False)]:
  hashes,meta,manifest=load(run,expected); cases=[c for c in manifest['cases'] if c['id'] in ids]; assert {c['id'] for c in cases}==ids
  raw=read(run/'raw.json'); m=read(run/'run-metadata.json'); identity=identity_matches(m,saved)
  checks={'recorded_fixture_hashes':m.get('fixture_sha256s')==hashes,'recorded_snapshot_manifest':m.get('snapshot_validation',{}).get('manifest_sha256')==sha(sm),'snapshot_path':Path(m.get('snapshot','')).resolve()==a.snapshot.resolve(),'version':m.get('template_version')=='investigation-v12','seed':m.get('seed')==7,'retries':m.get('retries')==0,'plan_origin':m.get('plan_origin')=='fixture_supplied','raw_case_ids':raw.get('case_ids')==[c['id'] for c in cases],'no_assessment':m.get('assessment') is None and raw.get('assessment') is None}
  entry={'fixture_sums_sha256':expected,'metadata_checks':checks,'code_identity':identity,'cases':{}}
  for case in cases:
   try: result=audit(run,case,hashes,meta,calc,answers,context,planned,reviewed,'v12',consumed=flat)
   except Exception as e: result={'status':'audit_error','errors':[type(e).__name__+': '+str(e)]}
   rp=run/'attempts'/case['id']/'raw'/'result.json'; result.setdefault('errors',[])
   if raw.get('outputs',{}).get(case['id'])!=(read(rp) if rp.exists() else None): result['errors'].append('raw_result_mismatch')
   entry['cases'][case['id']]=result
  report['runs'][run.name]=entry
 a.output.mkdir(parents=True,exist_ok=False)
 with (a.output/'integrity.json').open('x') as f: json.dump(report,f,indent=2,ensure_ascii=False);f.write('\n')
if __name__=='__main__': main()
