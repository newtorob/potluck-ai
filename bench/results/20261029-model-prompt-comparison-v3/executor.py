"""Execute the frozen four-arm comparison sequentially, retaining every failure."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
ENGINE = ROOT / 'apps/desktop/python'

parser = argparse.ArgumentParser()
parser.add_argument('--small-model', type=Path, required=True)
parser.add_argument('--large-model', type=Path, required=True)
parser.add_argument('--log-dir', type=Path, required=True)
args = parser.parse_args()
frozen = json.loads((OUT / 'frozen-code.json').read_text())
models = [
    ('3b', args.small_model, '9c9f56a391a3abbd5b89d0245bf6106081bcc3173119d4229235dd9d23253f94'),
    ('8b', args.large_model, 'd98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785'),
]
results = []
for name, path, sha in models:
    for prompt in ('v4', 'v5'):
        for file, expected in frozen.items():
            if hashlib.sha256((ROOT / file).read_bytes()).hexdigest() != expected:
                raise RuntimeError('Frozen source changed: ' + file)
        arm = name + '-' + prompt
        command = [str(ENGINE / '.venv/bin/python'), '-m', 'potluck_local.services.company_brain_grounding_runner',
                   '--suite', 'fresh', '--fixture-dir', str(ENGINE / 'evals/company_brain/model_comparison_v3'),
                   '--template-version', 'investigation-' + prompt, '--model-path', str(path),
                   '--model-sha256', sha, '--context-size', '8192', '--threads', '8', '--output-dir', str(OUT / arm)]
        print('Starting ' + arm, flush=True)
        with (args.log_dir / (arm + '.log')).open('x') as log:
            completed = subprocess.run(command, cwd=ENGINE, stdout=log, stderr=subprocess.STDOUT)
        results.append({'arm': arm, 'returncode': completed.returncode, 'command': command})
        with (OUT / (arm + '-execution.json')).open('x') as stream:
            json.dump(results[-1], stream, indent=2)
        print('Completed ' + arm + ': exit ' + str(completed.returncode), flush=True)
with (OUT / 'execution-summary.json').open('x') as stream:
    json.dump(results, stream, indent=2)
