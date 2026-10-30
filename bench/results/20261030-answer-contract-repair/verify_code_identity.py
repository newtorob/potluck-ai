from pathlib import Path
import json,hashlib,subprocess,argparse
p=argparse.ArgumentParser();p.add_argument('--tree',choices=['index','HEAD'],required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--manifest',type=Path);a=p.parse_args()
w=Path(__file__).resolve().parent;r=Path('/Users/rob/Documents/potluck');expected=json.loads((a.manifest or w/'candidate-final/snapshot.json').read_text())['production_hashes']
if a.tree=='index':names=subprocess.check_output(['git','ls-files','apps/desktop/python/potluck_local'],cwd=r,text=True).splitlines();ref=''
else:names=subprocess.check_output(['git','ls-tree','-r','--name-only','HEAD','apps/desktop/python/potluck_local'],cwd=r,text=True).splitlines();ref='HEAD'
names={n for n in names if n.endswith('.py')};missing=sorted(set(expected)-names);extra=sorted(names-set(expected));changed=[]
for n,h in expected.items():
 if n in names and hashlib.sha256(subprocess.check_output(['git','show',ref+':'+n],cwd=r)).hexdigest()!=h:changed.append(n)
result={'tree':a.tree,'expected_production_files':len(expected),'recorded_production_files':len(names),'missing':missing,'extra':extra,'changed':changed,'matches':not(missing or extra or changed)}
if a.tree=='HEAD':result['commit']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=r,text=True).strip()
a.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result));raise SystemExit(not result['matches'])
