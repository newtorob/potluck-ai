import json, os, pathlib, subprocess

work = pathlib.Path(__file__).resolve().parent
repo = pathlib.Path('/Users/rob/Documents/potluck')
snapshot = work / 'candidate-frozen/apps/desktop/python'
fixtures = repo / 'apps/desktop/python/evals/company_brain/reviewed_operations_v1'
outroot = repo / 'bench/results/20261029-reviewed-operations-v1'
outroot.mkdir(exist_ok=False)
contract = {
    'purpose': 'Compare answer workflow, supplied-plan arithmetic, and automatic-plan diagnostics separately.',
    'matrix': [['v6', 'auto'], ['v9', 'fixture_supplied'], ['v9', 'auto']],
    'seed': 7, 'cases_per_arm': 6, 'retries': 0,
    'rules': ['All attempts retained; failures count.', 'No post-holdout tuning.',
              'Fixture plans are not actual human review.', 'No autonomous or production quality claim from supplied-plan results.',
              'Primary and independent reviewers assess semantic correctness and evidence completeness.'],
    'fixture_manifest_sha256': '25915255a89ab0260f6b46f8e78571a2ef992da581df7e192c2b46271c490410',
    'fixture_author_limit': 'Author did not inspect v9 implementation but had seen the prior consumed corpus.',
    'snapshot': json.loads((work / 'candidate-frozen/snapshot.json').read_text()),
}
(outroot / 'evaluation-contract.json').write_text(json.dumps(contract, indent=2) + '\n')
env = os.environ.copy()
env['PYTHONPATH'] = str(snapshot)
env.pop('LLAMA_CPP_LIB_PATH', None)
for version, mode in contract['matrix']:
    label = f'{version}-{mode}-s7'
    out = outroot / label
    cmd = [str(repo / 'apps/desktop/python/.venv/bin/python'), '-m',
           'potluck_local.services.company_brain_grounding_runner', '--suite', 'fresh',
           '--fixture-dir', str(fixtures), '--template-version', f'investigation-{version}',
           '--operation-mode', mode, '--generation-profile', 'qwen3-non-thinking', '--seed', '7',
           '--model-path', '/Users/rob/.potluck/models/qwen3-8b-q4/model.gguf',
           '--model-sha256', 'd98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785',
           '--context-size', '8192', '--threads', '8', '--output-dir', str(out)]
    print('Starting', label, flush=True)
    with (outroot / f'{label}.log').open('x') as log:
        result = subprocess.run(cmd, cwd=snapshot, env=env, stdout=log, stderr=subprocess.STDOUT)
    print('Finished', label, 'exit', result.returncode, flush=True)
    if result.returncode:
        (outroot / f'{label}-process-outcome.json').write_text(json.dumps({'exit_code': result.returncode}) + '\n')
        raise SystemExit(result.returncode)
