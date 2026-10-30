import os,json,subprocess,pathlib
w=pathlib.Path(__file__).resolve().parent;repo=pathlib.Path('/Users/rob/Documents/potluck');out=repo/'bench/results/20261030-planned-answers-v1/development';snapshot=w/'candidate-dev2';env=os.environ.copy();env.pop('LLAMA_CPP_LIB_PATH',None);env['PYTHONPATH']=str(snapshot/'apps/desktop/python');results=[]
for name,runner in [('real-v12-dev2','run_real_development.py')]:
 command=[str(repo/'apps/desktop/python/.venv/bin/python'),str(w/runner),'--snapshot',str(snapshot/'apps/desktop/python'),'--snapshot-manifest',str(snapshot/'snapshot.json'),'--model','/Users/rob/.potluck/models/qwen3-8b-q4/model.gguf','--sha256','d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785','--version','v12','--output',str(out/name)]
 print('Starting consumed',name,flush=True)
 with (out/(name+'.log')).open('x') as f:r=subprocess.run(command,cwd=snapshot/'apps/desktop/python',env=env,stdout=f,stderr=subprocess.STDOUT)
 results.append({'name':name,'command':command,'exit_code':r.returncode});(out/'process-outcomes-dev2.json').write_text(json.dumps(results,indent=2)+'\n');print('Finished',name,r.returncode,flush=True)
raise SystemExit(any(r['exit_code'] for r in results))
