import json
import os
import pathlib
import subprocess

w = pathlib.Path(__file__).resolve().parent
repo = pathlib.Path('/Users/rob/Documents/potluck')
out = repo / 'bench/results/20261030-planned-answers-v1'
env = os.environ.copy()
env.pop('LLAMA_CPP_LIB_PATH', None)
results = []
for version, name in [('v11', 'baseline-frozen'), ('v12', 'candidate-frozen')]:
    snapshot = w / name
    engine = snapshot / 'apps/desktop/python'
    model_env = {**env, 'PYTHONPATH': str(engine)}
    command = [str(repo / 'apps/desktop/python/.venv/bin/python'), str(w / 'run_fresh.py'),
        '--snapshot', str(engine), '--snapshot-manifest', str(snapshot / 'snapshot.json'),
        '--fixture-dir', str(repo / 'apps/desktop/python/evals/company_brain/planned_answers_v1'),
        '--model', '/Users/rob/.potluck/models/qwen3-8b-q4/model.gguf',
        '--sha256', 'd98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785',
        '--version', version, '--output', str(out / ('8b-' + version))]
    print('Starting frozen', version, flush=True)
    with (out / ('8b-' + version + '.log')).open('x') as log:
        result = subprocess.run(command, cwd=engine, env=model_env, stdout=log, stderr=subprocess.STDOUT)
    results.append({'name': version, 'command': command, 'exit_code': result.returncode})
    (out / 'process-outcomes.json').write_text(json.dumps(results, indent=2) + '\n')
    print('Finished frozen', version, result.returncode, flush=True)
print('Inference complete; starting regression checks', flush=True)
checks = []
for name, command in [('sidecar', ['make', 'test-sidecar']), ('fast', ['make', 'test-fast'])]:
    check_env = {**env, 'PYTHONPATH': str(repo / 'apps/desktop/python')}
    with (w / (name + '-final.log')).open('x') as log:
        result = subprocess.run(command, cwd=repo, env=check_env, stdout=log, stderr=subprocess.STDOUT)
    checks.append({'name': name, 'command': command, 'exit_code': result.returncode})
    (w / 'checks.json').write_text(json.dumps(checks, indent=2) + '\n')
    print('Finished check', name, result.returncode, flush=True)
raise SystemExit(any(x['exit_code'] for x in results + checks))
