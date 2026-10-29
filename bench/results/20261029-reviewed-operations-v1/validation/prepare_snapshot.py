import hashlib, io, json, pathlib, subprocess, sys, tarfile

repo = pathlib.Path('/Users/rob/Documents/potluck')
work = pathlib.Path(__file__).resolve().parent
paths = [
    'apps/desktop/python/potluck_local/services/investigation_calculations.py',
    'apps/desktop/python/potluck_local/services/investigation_quantities.py',
    'apps/desktop/python/potluck_local/services/investigation_reviewed_operations.py',
    'apps/desktop/python/potluck_local/services/investigations.py',
    'apps/desktop/python/potluck_local/routers/investigations.py',
    'apps/desktop/python/potluck_local/services/company_brain_grounding_runner.py',
    'apps/desktop/python/potluck_local/services/company_brain_eval_runner.py',
    'apps/desktop/python/tests/test_investigation_quantities.py',
    'apps/desktop/python/tests/test_investigation_reviewed_operations.py',
    'apps/desktop/python/tests/test_investigation_reviewed_workflow.py',
    'apps/desktop/python/tests/test_investigations.py',
    'apps/desktop/python/tests/test_company_brain_grounding_runner.py',
    'apps/desktop/python/COMPANY_BRAIN.md',
]
baseline = json.loads((work.parent / 'cited-calculations/baseline-hashes.json').read_text())
assert not set(paths) & set(baseline)
changed = [rel for rel, sha in baseline.items() if not (repo / rel).is_file()
           or hashlib.sha256((repo / rel).read_bytes()).hexdigest() != sha]
assert not changed, changed
out = work / sys.argv[1]
out.mkdir(exist_ok=False)
archive = subprocess.check_output(['git', 'archive', 'HEAD', 'apps/desktop/python'], cwd=repo)
with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
    tar.extractall(out, filter='data')
for name in paths:
    p = out / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes((repo / name).read_bytes())
engine = out / 'apps/desktop/python'
hashes = {str(p.relative_to(engine)): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in sorted((engine / 'potluck_local').rglob('*.py'))}
(out / 'snapshot.json').write_text(json.dumps({
    'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip(),
    'overlays': paths, 'python_source_sha256': hashes,
    'preexisting_files_verified_unchanged': len(baseline),
}, indent=2) + '\n')
(work / 'owned-python-files.json').write_text(json.dumps(paths, indent=2) + '\n')
print(out, len(hashes), 'production source files;', len(baseline), 'preexisting paths preserved')
