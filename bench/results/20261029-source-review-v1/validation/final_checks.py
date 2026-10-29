import json, pathlib, subprocess

work = pathlib.Path(__file__).resolve().parent
repo = pathlib.Path('/Users/rob/Documents/potluck')
results = []
checks = [
    ('sidecar', ['env', 'PYTHONPATH=' + str(repo / 'apps/desktop/python'), 'make', 'test-sidecar'], repo),
    ('fast', ['make', 'test-fast'], repo),
    ('frontend-build', ['pnpm', 'run', 'build'], repo / 'apps/desktop/frontend'),
    ('frontend-format', ['pnpm', 'exec', 'prettier', '--check',
        'src/views/InvestigationsView.tsx', 'src/views/InvestigationsView.test.tsx',
        'src/types/investigations.ts'], repo / 'apps/desktop/frontend'),
    ('frontend-lint', ['pnpm', 'exec', 'eslint', 'src/views/InvestigationsView.tsx',
        'src/views/InvestigationsView.test.tsx', 'src/types/investigations.ts',
        'src/lib/localApi.ts'], repo / 'apps/desktop/frontend'),
]
for name, command, directory in checks:
    print('Starting', name, flush=True)
    with (work / f'{name}-final.log').open('x') as log:
        completed = subprocess.run(command, cwd=directory, stdout=log, stderr=subprocess.STDOUT)
    results.append({'name': name, 'command': command, 'exit_code': completed.returncode})
    (work / 'checks.json').write_text(json.dumps(results, indent=2) + '\n')
    print('Finished', name, completed.returncode, flush=True)
raise SystemExit(any(x['exit_code'] for x in results))
