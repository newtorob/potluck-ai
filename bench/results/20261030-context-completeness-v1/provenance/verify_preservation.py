import hashlib,json,pathlib,subprocess
w=pathlib.Path(__file__).resolve().parent
repo=pathlib.Path('/Users/rob/Documents/potluck')
initial=json.loads((w/'baseline-hashes.json').read_text())
# Restore only the generated build-info artifact captured before this batch.
build=repo/'apps/desktop/frontend/tsconfig.app.tsbuildinfo'
saved=w/'initial-tsconfig.app.tsbuildinfo'
build_restored=build.read_bytes()!=saved.read_bytes()
if build_restored: build.write_bytes(saved.read_bytes())
changed=[];missing=[]
for name,digest in initial.items():
 p=repo/name
 if not p.is_file():missing.append(name)
 elif hashlib.sha256(p.read_bytes()).hexdigest()!=digest:changed.append(name)
report={'preexisting_paths':len(initial),'changed':changed,'missing':missing,'build_info_restored_to_turn_start':build_restored}
(w/'preservation.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
raise SystemExit(bool(changed or missing))
