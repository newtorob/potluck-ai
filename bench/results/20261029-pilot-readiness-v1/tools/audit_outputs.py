#!/usr/bin/env python3
"""Read-only artifact integrity audit; never loads a model or grades answers."""
import argparse, hashlib, json, sys
from pathlib import Path

def read(p): return json.loads(p.read_text())
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,obj):
    with p.open('x',encoding='utf-8') as f: json.dump(obj,f,indent=2,ensure_ascii=False,default=str); f.write('\n')
def canonical(x): return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def check_frozen(root):
    d=root/'frozen'; entries={}
    for line in (d/'SHA256SUMS').read_text().splitlines():
        h,name=line.split('  ',1); p=(d/name).resolve(strict=True)
        if not p.is_relative_to(d.resolve()) or name in entries or digest(p)!=h: raise ValueError('Frozen hash mismatch: '+name)
        entries[name]=h
    metadata=read(d/'sources.json')
    for s in metadata['sources']:
        if entries.get(s['file']) != s['sha256']: raise ValueError('Source metadata hash mismatch: '+s['file'])
    return entries,read(d/'manifest.json')
def attempt(root,cid,calc):
    folder=root/'attempts'/cid/'raw'; report={'case_id':cid,'errors':[],'requests':[],'snippets':0,'catalog_reconstructed':None,'traces_replayed':0}
    result_path=folder/'result.json'
    data=read(result_path) if result_path.exists() else {'status':'missing_result'}
    report['status']=data.get('status'); item=data.get('result')
    requests=[]
    for path in sorted((folder/'calls').glob('*/request.json')):
        obj=read(path); report['requests'].append({'file':str(path.relative_to(root)),'sha256':digest(path)})
        for msg in obj.get('messages',[]):
            if msg.get('role')=='user':
                try:
                    payload=json.loads(msg['content'])
                    if isinstance(payload,dict): requests.append(payload)
                except (ValueError,TypeError): pass
    if not item: return report,{'status':data.get('status'),'error':data.get('error')},{'evidence':None,'payloads':requests}
    p=item.get('provenance',{}); snippets=p.get('snippet_catalog',[]); evidence=item.get('evidence',[])
    context={e['citation_id']:e.get('presented_content','') for e in evidence}
    for s in snippets:
        if not s.get('quote') or s['quote'] not in context.get(s.get('citation_id'),''): report['errors'].append('Snippet outside presented evidence: '+str(s.get('snippet_id')))
        report['snippets']+=1
    by_sid={s['snippet_id']:s for s in snippets}
    for a in item.get('brief',{}).get('answers',[]):
        expected=[by_sid.get(s) for s in a.get('snippet_ids',[])]
        if None in expected or a.get('source_excerpts',[])!=expected: report['errors'].append('Answer snippet binding mismatch')
    try:
        version=p.get('quantity_parser_version','v2')
        if version!='v3': report['errors'].append('Unexpected parser version '+str(version))
        catalog=calc.quantity_catalog(snippets,source_context={e['citation_id']:e['content'] for e in evidence},version=version)
        notices=calc.extraction_notices(snippets,catalog,source_context={e['citation_id']:e['content'] for e in evidence},version=version)
        report['catalog_reconstructed']=(catalog==p.get('quantity_catalog') and notices==p.get('extraction_notices'))
        if not report['catalog_reconstructed']: report['errors'].append('Catalog or notices mismatch')
        for trace in p.get('calculation_traces',[]):
            if trace.get('status')=='computed':
                rebuilt=calc.calculate(trace['operation'],[q['id'] for q in trace['inputs']],catalog,version=version)
                # Compare replayed core fields only; extra annotations in v10 are allowed.
                if any(trace.get(k)!=v for k,v in rebuilt.items()): report['errors'].append('Calculation replay mismatch '+str(trace.get('request_id')))
                else: report['traces_replayed']+=1
    except Exception as e: report['errors'].append('Reconstruction error: '+type(e).__name__+': '+str(e))
    answer={'status':'success','answers':item.get('brief',{}).get('answers',[]),'questions':item.get('brief',{}).get('questions',[])}
    evidence_normalized=sorted([{'path':e.get('relative_path'),'presented_content':e.get('presented_content','')} for e in evidence],key=canonical)
    return report,answer,{'evidence':evidence_normalized,'payloads':requests}
def normalize(payloads):
    if not isinstance(payloads,dict): return None
    # Preserve user payload exactly, including order, aliases, quantities and notices.
    return {'evidence':payloads['evidence'],'payloads':payloads['payloads']}
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--arm-a',type=Path,required=True); ap.add_argument('--arm-b',type=Path,required=True); ap.add_argument('--snapshot',type=Path,required=True); ap.add_argument('--output',type=Path,required=True); a=ap.parse_args()
    sys.path.insert(0,str(a.snapshot.resolve(strict=True)))
    from potluck_local.services import investigation_calculations as calc
    roots=[a.arm_a,a.arm_b]; frozen=[check_frozen(r) for r in roots]; metas=[read(r/'run-metadata.json') for r in roots]
    if frozen[0][0]!=frozen[1][0] or frozen[0][1]!=frozen[1][1]: raise ValueError('Frozen fixtures differ')
    if metas[0]['model_sha256']!=metas[1]['model_sha256']: raise ValueError('Model hashes differ')
    manifest=frozen[0][1]; output=a.output.absolute(); output.mkdir(exist_ok=False)
    report={'model_sha256':metas[0]['model_sha256'],'frozen_files_sha256':frozen[0][0],'source_sets_equal':True,'case_results':{},'no_semantic_scoring':True,'notes':['Exact canonical user JSON payloads preserve order, identifiers, quantities and notices; system instructions and schema are excluded. Presented evidence separately normalizes citation IDs to paths and exact text.','Only saved presented evidence supports snippet checks; failures and missing results remain in the report.']}; answers={}
    for c in manifest['cases']:
        cid=c['id']; runs=[attempt(r,cid,calc) for r in roots]
        expected={Path(n).name:frozen[0][0][n] for n in c['sources']}
        for i,root in enumerate(roots):
            reg=root/'attempts'/cid/'raw'/'registration.json'
            if reg.exists():
                observed=read(reg).get('files',{})
                actual={name:digest(root/'attempts'/cid/'raw'/'allowed'/name) for name in observed}
                runs[i][0]['registered_sources_match'] = observed == expected and actual == expected
                if not runs[i][0]['registered_sources_match']: runs[i][0]['errors'].append('Registered source files mismatch')
            else:
                runs[i][0]['registered_sources_match']=False
                runs[i][0]['errors'].append('Registration missing')
        normalized=[normalize(x[2]) for x in runs]
        report['case_results'][cid]={'arms':[x[0] for x in runs],'user_payload_equal':normalized[0]['payloads']==normalized[1]['payloads'] if all(x is not None for x in normalized) and all(x['payloads'] for x in normalized) else None,'presented_evidence_equal':normalized[0]['evidence']==normalized[1]['evidence'] if all(x is not None and x['evidence'] is not None for x in normalized) else None,'normalized_inputs':normalized}
        answers[cid]={'question':c['question'],'source_set':c['sources'],'arm_a':runs[0][1],'arm_b':runs[1][1]}
    write(output/'integrity.json',report); write(output/'answers.json',answers)
if __name__=='__main__': main()
