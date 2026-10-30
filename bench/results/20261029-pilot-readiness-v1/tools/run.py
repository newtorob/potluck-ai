#!/usr/bin/env python3
"""Frozen six-case local diagnostic. No retries, no assessments, no remote inference."""
import argparse, asyncio, hashlib, json, os, shutil, sys, time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

FIXTURES = Path('/Users/rob/Documents/potluck/apps/desktop/python/evals/company_brain/pilot_readiness_v1')
def sha(path):
    with path.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()
def utc(): return datetime.now(timezone.utc).isoformat()

def load_frozen(fixture_dir):
    global FIXTURES
    FIXTURES = fixture_dir.resolve(strict=True)
    entries = {}
    for line in (FIXTURES/'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ', 1)
        p = (FIXTURES/name).resolve(strict=True)
        if not p.is_relative_to(FIXTURES.resolve()) or sha(p) != digest or name in entries:
            raise ValueError('Invalid frozen file/hash: '+name)
        entries[name] = p
    manifest = json.loads(entries['manifest.json'].read_bytes())
    rubric = json.loads(entries['rubric.json'].read_bytes())
    metadata = json.loads(entries['sources.json'].read_bytes())
    cases = manifest['cases']
    ids = [c['id'] for c in cases]
    if len(ids) != 6 or len(set(ids)) != 6 or set(ids) != set(rubric['cases']): raise ValueError('Invalid case IDs')
    source_meta = {x['file']:x for x in metadata['sources']}
    for c in cases:
        if not all(ch in 'abcdefghijklmnopqrstuvwxyz0123456789-' for ch in c['id']): raise ValueError('Invalid case ID')
        if len(c['plan']) != 1 or c['plan'][0]['question_id'] != 'Q1': raise ValueError('Invalid plan')
        for name in c['sources']:
            if name not in entries or name not in source_meta or sha(entries[name]) != source_meta[name]['sha256']: raise ValueError('Invalid source '+name)
    return entries, manifest

async def run(args):
    entries, manifest = load_frozen(args.fixture_dir)
    if not args.model.is_file() or args.model.is_symlink() or sha(args.model) != args.sha256.lower(): raise ValueError('Model hash mismatch or invalid file')
    snapshot = args.snapshot.resolve(strict=True)
    if not (snapshot/'potluck_local/services/company_brain_eval_runner.py').is_file(): raise ValueError('Snapshot must be Python engine directory')
    sys.path.insert(0, str(snapshot))
    from potluck_local.services import company_brain_eval_runner as base, investigations, investigation_profiles, investigation_reviewed_operations
    from potluck_local.services.evidence_lookup import EvidenceScope
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.runtime import get_runtime
    from potluck_local.services.source_collections import register_collection, scan_collection
    # Validate before touching output/model; v10 must expose its own implementation in snapshot.
    plans = {c['id']: investigation_reviewed_operations.validate_rows([{k:r[k] for k in ('request','operation')} for r in c['plan']]) for c in manifest['cases']}
    out = args.output.absolute(); out = out.parent.resolve(strict=True)/out.name
    out.mkdir(mode=0o700, exist_ok=False); (out/'data').mkdir(mode=0o700); (out/'frozen').mkdir()
    for name,p in entries.items():
        target=out/'frozen'/name; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(p,target)
    shutil.copyfile(FIXTURES/'SHA256SUMS',out/'frozen'/'SHA256SUMS')
    settings=({'n_ctx':8192,'n_threads':8,'n_gpu_layers':0,'cpu_only':True,'rpc_servers':None} if args.backend == 'python' else {'n_ctx':8192,'n_threads':8,'n_gpu_layers':10,'cache_mib':0})
    bundle_info = None
    base.write_new(out/'run-metadata.json',{'started_at':utc(),'template_version':'investigation-'+args.version,'plan_origin':'fixture_supplied','seed':7,'generation_profile':investigation_profiles.resolve_profile('qwen3-non-thinking').metadata(),'model_path':str(args.model),'model_sha256':args.sha256.lower(),'settings':settings,'backend':args.backend,'native_bundle':bundle_info,'mode':'preflight' if args.preflight_only else 'cases','snapshot':str(snapshot),'code_identity':base.identity(),'fixture_sha256s':{k:sha(v) for k,v in entries.items()},'retries':0,'assessment':None,'retrieval_limitation':'Normal scoped lexical retrieval, context and result limits apply; no evidence is inserted or forced.'})
    with base.isolated_environment(POTLUCK_DATA_DIR=str(out/'data')):
        runtime=get_runtime()
        try:
            if args.backend == 'native':
                from potluck_local.services import local_server
                root, native_manifest = local_server.bundle()
                bundle_info = {'root':str(root), 'manifest':native_manifest, 'files_sha256':{str(p.relative_to(root)):sha(p) for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()}}
                base.write_new(out/'native-bundle.json',bundle_info)
            report=(runtime.load_model(str(args.model),**settings) if args.backend == 'python' else runtime._load_local_server(str(args.model),**settings,expected_model_sha256=args.sha256.lower()))
            base.write_new(out/'model-load.json',{'report':report,'inference_summary':runtime.inference_summary(),'actual_placement_note':'Native process discards engine logs; effective GPU placement is not independently observable here.' if args.backend == 'native' else 'See runtime report.'})
            if runtime.current_model_sha256()!=args.sha256.lower(): raise ValueError('Loaded model identity mismatch')
            model=runtime.current_model_id(); outputs={}
            if args.preflight_only:
                folder=out/'preflight'; folder.mkdir()
                base.write_new(folder/'started.json',{'started_at':utc()})
                schema={'type':'object','properties':{'status':{'type':'string','enum':['READY']},'note':{'type':'null'}},'required':['status','note'],'additionalProperties':False}
                messages=[{'role':'system','content':'Return the requested JSON.'},{'role':'user','content':'Set status to READY and note to null.'}]
                capture=base.CaptureRuntime(runtime,folder,{'purpose':'non-task schema/profile capability check'})
                start=time.monotonic()
                try:
                    reply=await capture.chat(messages,expected_model=model,expected_sha256=args.sha256.lower(),max_tokens=128,temperature=0,response_format={'type':'json_object','schema':schema},bounded_generation=True,generation_profile='qwen3-non-thinking',seed=7)
                    raw=reply['choices'][0]['message']['content']; parsed=json.loads(raw)
                    ok=parsed == {'status':'READY','note':None} and reply['choices'][0].get('finish_reason') != 'length'
                    base.write_new(folder/'result.json',{'status':'success' if ok else 'invalid_response','parsed':parsed,'finished_at':utc(),'elapsed_seconds':time.monotonic()-start})
                except Exception as e:
                    base.write_new(folder/'result.json',{'status':'error','error':{'type':type(e).__name__,'message':str(e)},'finished_at':utc(),'elapsed_seconds':time.monotonic()-start})
                return
            for case in manifest['cases']:
                folder=out/'attempts'/case['id']/'raw'; folder.mkdir(parents=True)
                base.write_new(folder/'started.json',{'case_id':case['id'],'started_at':utc()})
                started=time.monotonic(); investigation_start=None
                result={k:None for k in ('citation_validity','semantic_support','unsupported_answer','correction','time_to_accepted_seconds')}
                with base.isolated_environment(POTLUCK_MEMORY_DB_PATH=str(folder/'isolated.sqlite')):
                    try:
                        await init_memory_db(); allowed=folder/'allowed'; allowed.mkdir()
                        for name in case['sources']:
                            target=allowed/Path(name).name; shutil.copyfile(entries[name],target)
                            if sha(target)!=sha(entries[name]): raise ValueError('Copied source mismatch')
                        workspace='pilot-eval'; cid=await register_collection(workspace,'frozen',allowed); scan=await scan_collection(workspace,cid)
                        scope=EvidenceScope(workspace,(cid,))
                        base.write_new(folder/'registration.json',{'workspace':workspace,'collection_id':cid,'root':str(allowed),'files':{Path(n).name:sha(allowed/Path(n).name) for n in case['sources']},'scan':asdict(scan)})
                        original_runtime,original_lookup=investigations.get_runtime,investigations.lookup_evidence
                        count=0
                        async def capture(scope_arg,query,limit):
                            nonlocal count
                            count+=1; target=folder/f'retrieval-{count:03d}.json'
                            try:
                                value=await original_lookup(scope_arg,query,limit)
                                base.write_new(target,{'workspace':scope_arg.workspace,'collection_ids':list(scope_arg.collection_ids),'query':query,'limit':limit,'result':value})
                                return value
                            except Exception as e:
                                base.write_new(target,{'query':query,'limit':limit,'error':{'type':type(e).__name__,'message':str(e)}}); raise
                        investigations.get_runtime=lambda: base.CaptureRuntime(runtime,folder,{'operation_mode':'fixture_supplied','plan_origin':'fixture_supplied'})
                        investigations.lookup_evidence=capture
                        try:
                            investigation_start=time.monotonic()
                            item=await investigations.create_investigation(scope,case['question'],case['question'],model,template_version='investigation-'+args.version,generation_profile='qwen3-non-thinking',seed=7,reviewed_operations=plans[case['id']],reviewed_plan_origin='fixture_supplied')
                        finally:
                            investigations.get_runtime=original_runtime; investigations.lookup_evidence=original_lookup
                        result.update(status='success',result=item)
                    except Exception as e:
                        result.update(status='error',error={'type':type(e).__name__,'message':str(e)},failure_stage='inference_or_validation' if (folder/'request.json').exists() else 'pre_inference')
                    end=time.monotonic(); result.update(latency_seconds=end-investigation_start if investigation_start is not None else None,setup_seconds=(investigation_start or end)-started,attempt_total_seconds=end-started,finished_at=utc())
                    base.write_new(folder/'result.json',result); outputs[case['id']]=result
            base.write_new(out/'raw.json',{'case_ids':[c['id'] for c in manifest['cases']],'assessment':None,'outputs':outputs})
        except Exception as e:
            base.write_new(out/'run-error.json',{'type':type(e).__name__,'message':str(e),'finished_at':utc()}); raise
        finally: runtime.unload_model()

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--fixture-dir',type=Path,default=FIXTURES); p.add_argument('--backend',choices=['python','native'],default='python'); p.add_argument('--preflight-only',action='store_true'); p.add_argument('--snapshot',type=Path,required=True); p.add_argument('--model',type=Path,required=True); p.add_argument('--sha256',required=True); p.add_argument('--version',choices=['v9','v10'],required=True); p.add_argument('--output',type=Path,required=True); a=p.parse_args(); asyncio.run(run(a))
if __name__=='__main__': main()
