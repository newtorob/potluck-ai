#!/usr/bin/env python3
"""Audit declared consumed v11 artifacts; never run inference or assess quality."""
import argparse, json, sys
from pathlib import Path
from audit_outputs import audit, read, sha, identity_matches

def main():
 p=argparse.ArgumentParser(); p.add_argument('--run',type=Path,required=True); p.add_argument('--fresh-v11',type=Path,required=True); p.add_argument('--snapshot',type=Path,required=True); p.add_argument('--contract',type=Path,required=True); p.add_argument('--output',type=Path,required=True); a=p.parse_args()
 assert (a.run/'raw.json').is_file(), 'completed raw.json required'
 d=a.run/'frozen'; hashes={}
 for line in (d/'SHA256SUMS').read_text().splitlines():
  h,n=line.split('  ',1); file=(d/n).resolve(strict=True)
  assert file.is_relative_to(d.resolve()) and n not in hashes and sha(file)==h, n
  hashes[n]=h
 metadata=read(d/'sources.json')
 for s in metadata['sources']: assert hashes.get(s['file'])==s['sha256'],s['file']
 manifest=read(d/'manifest.json'); selected=[c for c in manifest['cases'] if c['id'] in ('pilot-readiness','core-company')]
 assert len(selected)==2 and len({c['id'] for c in selected})==2
 raw=read(a.run/'raw.json'); runmeta=read(a.run/'run-metadata.json'); contract=read(a.contract); freshmeta=read(a.fresh_v11/'run-metadata.json')
 assert sha(d/'SHA256SUMS')==contract['consumed_real_diagnostic']['fixture_sums_sha256'], 'consumed fixture checksum manifest differs from declared contract'
 sys.path.insert(0,str(a.snapshot)); from potluck_local.services import investigation_calculations as calc, investigation_answers as answers, investigation_context as context
 expected_snapshot=contract['snapshots']['candidate-frozen']['production_hashes']; snapshot_root=a.snapshot.parent.parent.parent
 mismatched=[n for n,h in expected_snapshot.items() if not (snapshot_root/n).is_file() or sha(snapshot_root/n)!=h]
 checks={'raw_case_ids':raw.get('case_ids')==[c['id'] for c in selected], 'fixture_hashes_match_metadata':runmeta.get('fixture_sha256s')==hashes,'version':runmeta.get('template_version')=='investigation-v11','plan_origin':runmeta.get('plan_origin')=='fixture_supplied','model_sha':runmeta.get('model_sha256')==contract.get('model_sha256'),'seed':runmeta.get('seed')==7,'retries':runmeta.get('retries')==0,'no_assessment':runmeta.get('assessment') is None and raw.get('assessment') is None,'snapshot_path':Path(runmeta.get('snapshot','')).resolve()==a.snapshot.resolve(),'snapshot_hashes':not mismatched}
 checks.update({'settings_match_fresh_v11':runmeta.get('settings')==freshmeta.get('settings'),'generation_profile_matches_fresh_v11':runmeta.get('generation_profile')==freshmeta.get('generation_profile'),'model_matches_fresh_v11':runmeta.get('model_sha256')==freshmeta.get('model_sha256'),'seed_matches_fresh_v11':runmeta.get('seed')==freshmeta.get('seed'),'retries_match_fresh_v11':runmeta.get('retries')==freshmeta.get('retries')})
 report={'purpose':'consumed real-source diagnostic, not a fresh holdout or controlled comparison','semantic_assessment':False,'notes':['Exact source and citation bindings do not establish semantic support.','Both declared cases remain in the denominator, including failed attempts.'],'fixture_sums_sha256':sha(d/'SHA256SUMS'),'fixture_sha256s':hashes,'recorded_metadata':runmeta,'metadata_checks':checks,'code_identity_check':identity_matches(runmeta,contract['snapshots']['candidate-frozen']),'snapshot_files_mismatched':mismatched,'cases':{}}
 for c in selected:
  try: x=audit(a.run,c,hashes,metadata,calc,answers,context,'v11',consumed=True)
  except Exception as e: x={'status':'audit_error','errors':[type(e).__name__+': '+str(e)]}
  result=a.run/'attempts'/c['id']/'raw'/'result.json'
  if raw.get('outputs',{}).get(c['id'])!=(read(result) if result.is_file() else None): x.setdefault('errors',[]).append('raw_result_mismatch')
  report['cases'][c['id']]=x
 a.output.mkdir(parents=True,exist_ok=False)
 with (a.output/'integrity.json').open('x') as f: json.dump(report,f,indent=2,ensure_ascii=False); f.write('\n')
if __name__=='__main__': main()
