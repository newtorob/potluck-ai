from pathlib import Path
import subprocess, os, json
w=Path(__file__).resolve().parent
repo=Path('/Users/rob/Documents/potluck')
out=repo/'bench/results/20261029-pilot-readiness-v1'
results=[]
for version, snapshot in [('v9','baseline-frozen'),('v10','candidate-frozen')]:
 env=os.environ.copy(); env.pop('LLAMA_CPP_LIB_PATH',None); env['PYTHONPATH']=str(w/snapshot/'apps/desktop/python')
 cmd=[str(repo/'apps/desktop/python/.venv/bin/python'),str(w/'run.py'),'--snapshot',env['PYTHONPATH'],'--model','/Users/rob/.potluck/models/qwen3-8b-q4/model.gguf','--sha256','d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785','--version',version,'--output',str(out/('8b-'+version))]
 print('Starting',version,flush=True)
 with (out/('8b-'+version+'.log')).open('x') as log: p=subprocess.run(cmd,env=env,cwd=w/snapshot/'apps/desktop/python',stdout=log,stderr=subprocess.STDOUT)
 results.append({'version':version,'exit_code':p.returncode}); (out/'process-outcomes.json').write_text(json.dumps(results,indent=2)+'\n')
 print('Finished',version,p.returncode,flush=True)
