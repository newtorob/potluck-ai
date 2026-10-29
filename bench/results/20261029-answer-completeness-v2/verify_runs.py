"""Check recorded inputs, source boundaries and version parity; do not grade answers."""
import hashlib
import json
from pathlib import Path

repo = Path(__file__).resolve().parents[3]
summary = Path(__file__).resolve().parent
freeze = json.loads((summary / 'source-freeze.json').read_text())
fixture_dir = repo / 'apps/desktop/python/evals/company_brain/answer_completeness_v2'
cases = json.loads((fixture_dir / 'fresh.json').read_text())['cases']
report = {'checks': [], 'arms': {}}
paired_inputs = {}
for version, seed in [('v6', 7), ('v7', 7), ('v7', 29)]:
    label = f'{version}-s{seed}'
    run = repo / f'bench/results/20261029-answer-completeness-fresh-{label}'
    meta = json.loads((run / 'run-metadata.json').read_text())
    raw = json.loads((run / 'raw.json').read_text())
    assert meta['code_identity']['python_source_sha256'] == freeze['python_source_sha256']
    assert meta['suite_sha256'] == freeze['fixtures']['fresh.json']
    assert meta['rubric_sha256'] == freeze['fixtures']['rubric.json']
    for name in ('fresh.json', 'rubric.json'):
        assert hashlib.sha256((run / name).read_bytes()).hexdigest() == freeze['fixtures'][name]
    assert meta['template_version'] == f'investigation-{version}'
    assert meta['seed'] == seed and meta['question_mode'] == 'auto'
    assert meta['model_sha256'] == 'd98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785'
    assert meta['generation_profile']['name'] == 'qwen3-non-thinking'
    assert meta['settings'] == {'n_ctx': 8192, 'n_threads': 8, 'n_gpu_layers': 0,
                                'cpu_only': True, 'rpc_servers': None}
    assert raw['case_ids'] == [case['id'] for case in cases]
    saved = excerpts = exact_plans = 0
    times = []
    failures = []
    for case in cases:
        folder = run / 'attempts' / case['id'] / 'raw'
        result = json.loads((folder / 'result.json').read_text())
        assert result == raw['outputs'][case['id']]
        requests = [json.loads(path.read_text())
                    for path in sorted(folder.glob('calls/*/request.json'))]
        assert requests
        assert json.loads(requests[0]['messages'][1]['content']) == {'issue': case['question']}
        for request in requests:
            assert request['settings']['seed'] == seed
            assert request['settings']['bounded_generation'] is True
            assert request['settings']['generation_profile'] == 'qwen3-non-thinking'
        visible = json.dumps(requests) + (folder / 'retrieval.json').read_text() + json.dumps(result)
        assert 'CANARY-SILVER-482' not in visible
        if result['status'] != 'success':
            failures.append({'case_id': case['id'], 'error': result.get('error')})
            continue
        saved += 1
        item = result['result']
        brief, provenance = item['brief'], item['provenance']
        times.append(provenance['elapsed_ms'])
        assert item['issue'] == case['question']
        assert len(requests) == 2 and len(brief['questions']) == len(brief['answers'])
        payload = json.loads(requests[1]['messages'][1]['content'])
        supplied = {s['snippet_id']: s['text'] for s in payload['snippets']}
        catalog = {s['snippet_id']: s for s in provenance['snippet_catalog']}
        assert {sid: value['quote'] for sid, value in catalog.items()} == supplied
        assert provenance['generation_profile'] == meta['generation_profile']
        if seed == 7:
            paired_inputs.setdefault(case['id'], {})[version] = {
                'issue': payload['issue'], 'snippets': payload['snippets'],
                'settings': requests[1]['settings']}
        if version == 'v7':
            cursor = 0
            spans = [q['text'] for q in brief['questions']]
            assert len(spans) == len(set(spans))
            for span in spans:
                start = case['question'].find(span, cursor)
                assert start >= 0
                cursor = start + len(span)
            exact_plans += 1
        for question, answer in zip(brief['questions'], brief['answers']):
            assert question['id'] == answer['question_id']
            assert [s['snippet_id'] for s in answer['source_excerpts']] == answer['snippet_ids']
            for excerpt in answer['source_excerpts']:
                assert excerpt == catalog[excerpt['snippet_id']]
                assert excerpt['quote'] in case['allowed_source']
                excerpts += 1
    report['arms'][label] = {'attempts': len(cases), 'saved': saved,
        'exact_excerpts_checked': excerpts, 'exact_issue_plans_checked': exact_plans,
        'failures': failures, 'elapsed_ms': times}

paired = 0
for pair in paired_inputs.values():
    if set(pair) != {'v6', 'v7'}:
        continue
    assert pair['v6']['issue'] == pair['v7']['issue']
    assert pair['v6']['snippets'] == pair['v7']['snippets']
    for key in ('expected_model', 'expected_sha256', 'max_tokens', 'temperature',
                'bounded_generation', 'generation_profile', 'seed'):
        assert pair['v6']['settings'][key] == pair['v7']['settings'][key]
    paired += 1
report['paired_answer_inputs_checked'] = paired
report['checks'] = [
    'All arms match the frozen clean Python snapshot, fixtures, rubric, model and CPU settings',
    'Planner receives only the original issue; all generation calls are captured',
    'Selected source excerpts exactly match the presented snippets and allowed source text',
    'Saved v7 plans contain ordered distinct nonoverlapping original-issue spans',
    'Paired successful cases use identical issue, snippet catalog and generation settings',
    'Excluded canary does not appear in requests, retrieval, plan, answers or visible evidence',
    'These checks establish artifact integrity, not semantic quality or completeness',
]
(summary / 'integrity.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({label: {key: value for key, value in arm.items() if key != 'elapsed_ms'}
                  for label, arm in report['arms'].items()}, indent=2))
