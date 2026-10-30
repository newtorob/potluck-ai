from pathlib import Path
import subprocess,json,time,hashlib,shutil,os
w=Path(__file__).resolve().parent;r=Path('/Users/rob/Documents/potluck');b=r/'bench/results/20261030-answer-contract-repair';out=b/'validation';out.mkdir(exist_ok=True)
report={}
for name,cmd in [('sidecar',['make','test-sidecar']),('fast',['make','test-fast'])]:
 started=time.monotonic()
 with (out/(name+'.log')).open('w') as f:code=subprocess.call(cmd,cwd=r,stdout=f,stderr=subprocess.STDOUT,env={**os.environ,'PYTHONPATH':str(r/'apps/desktop/python')})
 report[name]={'exit_code':code,'elapsed_seconds':time.monotonic()-started}
 (out/'checks.json').write_text(json.dumps(report,indent=2)+'\n');print(name,report[name],flush=True)
 if code:raise SystemExit(code)
p=r/'apps/desktop/frontend/tsconfig.app.tsbuildinfo'; saved=w/'initial-tsconfig.app.tsbuildinfo'
restored=p.read_bytes()!=saved.read_bytes()
if restored:shutil.copyfile(saved,p)
original=json.loads((w/'baseline-hashes.json').read_text());changed=[];missing=[]
for name,digest in original.items():
 p=r/name
 if not p.is_file():missing.append(name)
 elif hashlib.sha256(p.read_bytes()).hexdigest()!=digest:changed.append(name)
preservation={'paths_checked':len(original),'changed':changed,'missing':missing,'restored_generated_buildinfo':restored}
(out/'preservation.json').write_text(json.dumps(preservation,indent=2)+'\n');print('preservation',preservation,flush=True)
if changed or missing:raise SystemExit(2)
