import json
import os
import pathlib
import subprocess
import time

w = pathlib.Path(__file__).resolve().parent
repo = pathlib.Path('/Users/rob/Documents/potluck')
out = repo / 'bench/results/20261030-context-completeness-v1'
while True:
    p = w / 'checks.json'
    if p.exists():
        try:
            checks = json.loads(p.read_text())
        except ValueError:
            checks = []
        if len(checks) == 2:
            if any(c['exit_code'] for c in checks):
                raise SystemExit('Regression failure: diagnostic not run')
            break
    time.sleep(3)
snapshot = w / 'candidate-frozen/apps/desktop/python'
env = os.environ.copy()
env.pop('LLAMA_CPP_LIB_PATH', None)
env['PYTHONPATH'] = str(snapshot)
command = [str(repo / 'apps/desktop/python/.venv/bin/python'), str(w / 'run_consumed.py'),
    '--snapshot', str(snapshot), '--model', '/Users/rob/.potluck/models/qwen3-8b-q4/model.gguf',
    '--sha256', 'd98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785',
    '--version', 'v11', '--output', str(out / 'consumed-real-v11')]
print('Starting declared consumed real-source diagnostic', flush=True)
with (out / 'consumed-real-v11.log').open('x') as log:
    result = subprocess.run(command, cwd=snapshot, env=env, stdout=log, stderr=subprocess.STDOUT)
(out / 'consumed-process-outcome.json').write_text(json.dumps({'command': command, 'exit_code': result.returncode}, indent=2) + '\n')
print('Finished consumed diagnostic', result.returncode, flush=True)
raise SystemExit(result.returncode)
