from pathlib import Path
import subprocess,json,shutil,hashlib,datetime
w=Path(__file__).resolve().parent;r=Path('/Users/rob/Documents/potluck');b=r/'bench/results/20261030-answer-contract-repair';py=r/'apps/desktop/python/.venv/bin/python'
assert (b/'development/v13-dev4/raw.json').exists()
owned=json.loads((w/'owned-files.json').read_text());dev=w/'candidate-dev4'
for n in owned:
 if n.endswith('.py'):assert (r/n).read_bytes()==(dev/n).read_bytes(),n
candidate=w/'candidate-frozen';shutil.copytree(dev,candidate)
m=json.loads((candidate/'snapshot.json').read_text());m.update(frozen_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),purpose='frozen v13 for new four-case qualification; no subsequent production/prompt tuning');(candidate/'snapshot.json').write_text(json.dumps(m,indent=2)+'\n')
fixture=r/'apps/desktop/python/evals/company_brain/bounded_answers_v1';fixture_hash=hashlib.sha256((fixture/'SHA256SUMS').read_bytes()).hexdigest()
contract={'runner_sha256':hashlib.sha256((w/'run.py').read_bytes()).hexdigest(),'frozen_at':m['frozen_at'],'base_head':m['base_head'],'snapshots':{n:json.loads((w/n/'snapshot.json').read_text()) for n in ['candidate-frozen']},'fixture_sha256':fixture_hash,'model_sha256':'d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785','model':'qwen3-8b-q4','hardware':'Apple M2 Pro 16GiB, CPU-only 8threads','context':8192,'seed':7,'profile':'qwen3-non-thinking','deadline_seconds':240,'timeout_scope':'shared planner and answer calls; cancellation cleanup may extend wall time','retries':0,'cases':4,'assessment':'All required facts plus directly attached supporting excerpts; failures remain in denominator. Status truth and prose completeness assessed separately.','comparison':'Absolute v13 qualification on four unseen cases. No fresh v12 baseline run; consumed development comparisons and earlier v12 results are separate and cannot establish a fresh-case uplift.','limitations':['Small synthetic cases, one seed; not large-corpus or real-company pilot qualification','Supplied plans cover all requested operations; automatic main-operation planning not assessed','Source-free factual part plans unreviewed; valid JSON/citations do not establish semantic completeness','No production or prompt changes once fresh execution begins']}
(b/'evaluation-contract.json').write_text(json.dumps(contract,indent=2)+'\n')
for version,snapshot in [('v13','candidate-frozen')]:
 args=[str(py),str(w/'run.py'),'--fixture-dir',str(fixture),'--fixture-sha',fixture_hash,'--purpose','fresh-four-case-qualification','--snapshot',str(w/snapshot/'apps/desktop/python'),'--snapshot-manifest',str(w/snapshot/'snapshot.json'),'--version',version,'--model','/Users/rob/.potluck/models/qwen3-8b-q4/model.gguf','--sha256',contract['model_sha256'],'--output',str(b/('fresh-'+version))]
 with (w/('fresh-'+version+'.log')).open('w') as f:
  code=subprocess.call(args,cwd=r,stdout=f,stderr=subprocess.STDOUT)
 if code:raise SystemExit(code)
 print(version,'complete',flush=True)
