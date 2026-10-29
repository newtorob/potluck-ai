import json, os, pathlib, subprocess

work = pathlib.Path(__file__).resolve().parent
repo = pathlib.Path('/Users/rob/Documents/potluck')
snapshot = work / 'candidate-frozen/apps/desktop/python'
fixtures = repo / 'apps/desktop/python/evals/company_brain/source_review_v1'
outroot = repo / 'bench/results/20261029-source-review-v1'
outroot.mkdir(exist_ok=False)
contract = {
    'purpose': 'Qualify new source inputs and limitations; scripted plans and reviews are not owner acceptance.',
    'matrix': [['v9', 'fixture_supplied']],
    'seed': 7, 'cases_per_arm': 4, 'retries': 0,
    'rules': ['All attempts retained; failures count.', 'No post-holdout tuning.',
              'Fixture plans are not actual human review.', 'No autonomous or production quality claim from supplied-plan results.',
              'Primary and independent reviewers assess semantic correctness and evidence completeness.'],
    'fixture_manifest_sha256': '2259df81555a377ae28a8369b12b2defe34e68452a18d77146c23f3177b89fa5',
    'fixture_author_limit': 'Fresh agent saw previous filenames, field shapes and case IDs and memory search prior project context; did not read prior prose/rubric or production answer implementation. Conflict fixture amended pre-execution to exercise a calculation; four cases frozen before running.',
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
