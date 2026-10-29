import hashlib, json, pathlib, re, sys

repo = pathlib.Path('/Users/rob/Documents/potluck')
work = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(work / 'candidate-frozen/apps/desktop/python'))
from potluck_local.services import investigation_calculations as calc

root = repo / 'bench/results/20261029-reviewed-operations-v1'
fixture = repo / 'apps/desktop/python/evals/company_brain/reviewed_operations_v1'
frozen = json.loads((fixture / 'fresh.json').read_text())
report = {'attempts': 0, 'saved_drafts': 0, 'exact_excerpts': 0, 'reconstructed_catalogs': 0,
          'replayed_calculations': 0, 'source_hashes_match': True, 'canary_leaks': [], 'arms': {}}
snapshot = json.loads((work / 'candidate-frozen/snapshot.json').read_text())
for label in ('v6-auto-s7', 'v9-fixture_supplied-s7', 'v9-auto-s7'):
    arm = root / label
    raw = json.loads((arm / 'raw.json').read_text())
    metadata = json.loads((arm / 'run-metadata.json').read_text())
    assert metadata['code_identity']['python_source_sha256'] == snapshot['python_source_sha256']
    report['arms'][label] = {'saved_drafts': 0, 'errors': [], 'latencies_seconds': []}
    for cid, attempt in raw['outputs'].items():
        report['attempts'] += 1
        report['arms'][label]['latencies_seconds'].append(attempt.get('latency_seconds'))
        case = next(c for c in frozen['cases'] if c['id'] == cid)
        canaries = re.findall(r'CANARY-[A-Z0-9-]+', case.get('excluded_source', ''))
        for canary in canaries:
            for path in (arm / 'attempts' / cid / 'raw').rglob('*.json'):
                if canary in path.read_text():
                    report['canary_leaks'].append(str(path.relative_to(root)))
        if attempt['status'] != 'success':
            report['arms'][label]['errors'].append(cid)
            continue
        report['saved_drafts'] += 1
        report['arms'][label]['saved_drafts'] += 1
        item = attempt['result']
        provenance = item['provenance']
        snippets = {s['snippet_id']: s for s in provenance['snippet_catalog']}
        assert all(s['quote'] in case['allowed_source'] for s in snippets.values())
        for answer in item['brief']['answers']:
            assert answer['source_excerpts'] == [snippets[s] for s in answer['snippet_ids']]
            report['exact_excerpts'] += len(answer['source_excerpts'])
        if label.startswith('v9'):
            expected_origin = 'fixture_supplied' if 'fixture' in label else 'planned_unreviewed'
            assert provenance['operation_plan_origin'] == expected_origin
            source_context, lengths = {}, {}
            for evidence in item['evidence']:
                for child in evidence['sources'] if evidence['kind'] == 'reviewed_claim' else [evidence]:
                    cid2 = child['citation_id']
                    length = len(child.get('presented_content', ''))
                    if length > lengths.get(cid2, 0):
                        source_context[cid2], lengths[cid2] = child['content'], length
            catalog = calc.quantity_catalog(list(snippets.values()), source_context=source_context, version='v2')
            assert catalog == provenance['quantity_catalog']
            assert calc.extraction_notices(list(snippets.values()), catalog, source_context=source_context) == provenance['extraction_notices']
            report['reconstructed_catalogs'] += 1
            for trace in provenance['calculation_traces']:
                if trace['status'] == 'computed':
                    result = calc.calculate(trace['operation'], [x['id'] for x in trace['inputs']], catalog, version='v2')
                    assert all(trace[key] == value for key, value in result.items())
                    report['replayed_calculations'] += 1
assert not report['canary_leaks']
(root / 'artifact-verification.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
