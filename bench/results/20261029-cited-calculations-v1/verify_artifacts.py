"""Verify recorded artifacts against the frozen production snapshot, without inference."""
import hashlib
import json
import pathlib
import sys

summary = pathlib.Path(__file__).resolve().parent
repo = summary.parents[2]
freeze = json.loads((summary / 'source-freeze.json').read_text())
source = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(freeze['snapshot']) / 'apps/desktop/python'
sys.path.insert(0, str(source))
from potluck_local.services.investigation_calculations import calculate, quantity_catalog

sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
assert {str(p.relative_to(source)): sha(p) for p in (source / 'potluck_local').rglob('*.py')} == freeze['python_source_sha256']
fixture_root = repo / 'apps/desktop/python/evals/company_brain/cited_calculations_v1'
for name, expected in freeze['fixtures'].items():
    assert sha(fixture_root / name) == expected
cases = {c['id']: c for c in json.loads((fixture_root / 'fresh.json').read_text())['cases']}
counts = {'attempts': 0, 'drafts': 0, 'errors': 0, 'visible_excerpts': 0, 'quantity_catalogs_rebuilt': 0, 'source_quantities': 0, 'computed_traces_replayed': 0, 'uncomputed_traces': 0, 'captured_calls': 0}
arm_reports = {}
for arm, version, seed in [('v6-s7', 'investigation-v6', 7), ('v8-s7', 'investigation-v8', 7), ('v8-s29', 'investigation-v8', 29)]:
    run = summary.parent / f'20261029-cited-calculations-fresh-{arm}'
    metadata = json.loads((run / 'run-metadata.json').read_text())
    assert metadata['code_identity']['python_source_sha256'] == freeze['python_source_sha256']
    assert metadata['suite_sha256'] == freeze['fixtures']['fresh.json']
    assert metadata['rubric_sha256'] == freeze['fixtures']['rubric.json']
    assert metadata['model_sha256'] == freeze['model_sha256']
    assert metadata['seed'] == seed and metadata['template_version'] == version
    assert metadata['settings'] == {'n_ctx':8192,'n_threads':8,'n_gpu_layers':0,'cpu_only':True,'rpc_servers':None}
    assert metadata['generation_profile']['enable_thinking'] is False
    assert metadata['generation_profile']['temperature'] == 0.7
    assert metadata['generation_profile']['template_sha256'] == '57f1fd00f0013a2be96aa79b857391f27e23df5b5f847072b524c897e24d0361'
    assert json.loads((run/'model-load.json').read_text())['inference']['execution_verified'] is True
    raw = json.loads((run / 'raw.json').read_text())
    assert set(raw['outputs']) == set(cases)
    statuses = {}
    for cid, output in raw['outputs'].items():
        counts['attempts'] += 1
        statuses[cid] = output['status']
        attempt = run / 'attempts' / cid / 'raw'
        assert json.loads((attempt/'result.json').read_text()) == output
        assert (attempt/'allowed/source.md').read_text() == cases[cid]['allowed_source']
        retrieval = json.loads((attempt/'retrieval.json').read_text())
        context = {r['citation_id']:r['content'] for r in retrieval['result']['source_chunks']}
        calls = sorted((attempt/'calls').glob('*/request.json'))
        assert 1 <= len(calls) <= 2
        counts['captured_calls'] += len(calls)
        checked = [attempt/'request.json', attempt/'retrieval.json', attempt/'result.json', *calls, *sorted((attempt/'calls').glob('*/raw-reply.json'))]
        for path in checked:
            assert 'CANARY-INDIGO-731' not in path.read_text(), str(path)
        if output['status'] != 'success':
            counts['errors'] += 1
            continue
        counts['drafts'] += 1
        result = output['result']
        assert result['status'] == 'draft' and result['review'] is None
        for answer in result['brief']['answers']:
            for excerpt in answer['source_excerpts']:
                assert excerpt['citation_id'] in context
                assert excerpt['quote'] in context[excerpt['citation_id']]
                assert excerpt['quote'] in cases[cid]['allowed_source']
                counts['visible_excerpts'] += 1
        if version == 'investigation-v8':
            provenance = result['provenance']
            quantities = provenance['quantity_catalog']
            assert quantity_catalog(provenance['snippet_catalog'], source_context=context) == quantities
            counts['quantity_catalogs_rebuilt'] += 1
            counts['source_quantities'] += len(quantities)
            for trace in provenance['calculation_traces']:
                answer = next(a for a in result['brief']['answers'] if a['question_id'] == trace['request_id'])
                assert answer['calculation'] == trace
                for operand in trace['inputs']:
                    assert operand in quantities
                    assert operand['snippet_text'][operand['start']:operand['end']] == operand['source_text']
                    if 'unit_source_text' in operand:
                        assert operand['snippet_text'][operand['unit_source_start']:operand['unit_source_end']] == operand['unit_source_text']
                    assert operand['snippet_id'] in answer['snippet_ids']
                if trace['status'] == 'computed':
                    replay = calculate(trace['operation'], [n['id'] for n in trace['inputs']], quantities)
                    assert {k:v for k,v in trace.items() if k not in ('request_id','operation')} == replay
                    counts['computed_traces_replayed'] += 1
                else:
                    counts['uncomputed_traces'] += 1
    arm_reports[arm] = statuses
report = {'status':'pass', 'checks':counts, 'arms':arm_reports, 'source_files':len(freeze['python_source_sha256']), 'limits':'Structural provenance and deterministic replay only. Semantic coverage, operand roles, authority and evidence relevance require separate human-style assessment. No inference or owner acceptance performed.'}
(summary/'artifact-verification.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
