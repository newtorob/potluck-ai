import asyncio
import copy
import json
from types import SimpleNamespace
from fastapi.testclient import TestClient
from potluck_local.app import create_app
from potluck_local.services import investigations as inv
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.source_collections import register_collection, scan_collection


def test_preview_review_export(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('POTLUCK_SIDECAR_TOKEN', raising=False)
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    calls = []
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            calls.append(1)
            return {'choices': [{'message': {'content': json.dumps({'R1': {'status': 'ready', 'operand_ids': ['N1', 'N2'], 'snippet_ids': [], 'reason': 'Selected.'}})}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    asyncio.run(init_memory_db())
    folder = tmp_path / 'docs'
    folder.mkdir()
    (folder / 'release.md').write_text('# Release\nLatency changed from 100 to 200 ms.')
    cid = asyncio.run(register_collection('w', 'docs', folder))
    asyncio.run(scan_collection('w', cid))
    scope = {'workspace': 'w', 'collection_ids': [cid]}
    client = TestClient(create_app(), client=('127.0.0.1', 44444))
    response = client.post('/company-brain/investigations', json={**scope, 'issue': 'Latency?', 'query': 'release', 'model_slug': 'model', 'reviewed_operations': [{'request': 'Latency change?', 'operation': 'difference'}], 'plan_confirmed': True})
    assert response.status_code == 200, response.text
    item = response.json()
    url = '/company-brain/investigations/' + item['id']
    row = {'request_id': 'R1', 'status': 'calculate', 'operand_ids': ['N2', 'N1'], 'snippet_ids': [], 'reason': 'Reviewed order.'}
    for bad in ({**row, 'operand_ids': ['N2', 'N99']}, {**row, 'value': 100}, {**row, 'request_id': 'R9'}, {**row, 'operand_ids': ['N1', 'N1']}):
        assert client.post(url + '/calculation-preview', json={**scope, 'calculation_reviews': [bad]}).status_code in (400, 422)
    preview = client.post(url + '/calculation-preview', json={**scope, 'calculation_reviews': [row]})
    assert preview.status_code == 200, preview.text
    answer = preview.json()['answers'][0]
    assert answer['calculation']['result'] == '-0.1' and answer['source_excerpts']
    limitation = client.post(url + '/calculation-preview', json={**scope, 'calculation_reviews': [{**row, 'status': 'missing', 'operand_ids': [], 'snippet_ids': answer['snippet_ids']}]})
    assert 'Reviewer assessment' in limitation.json()['answers'][0]['text']
    for status in ('unsupported', 'conflicting'):
        response = client.post(url + '/calculation-preview', json={**scope, 'calculation_reviews': [
            {**row, 'status': status, 'operand_ids': [], 'snippet_ids': answer['snippet_ids']}]})
        assert response.status_code == 200
        assert response.json()['answers'][0]['calculation']['assessment'] == 'reviewer assessment'
    assert client.post(url + '/calculation-preview', json={**scope, 'workspace': 'wrong', 'calculation_reviews': [row]}).status_code == 404
    correction = copy.deepcopy(item['brief'])
    correction['answers'][0] = answer
    bad = copy.deepcopy(correction)
    bad['answers'][0]['text'] = 'changed'
    payload = {**scope, 'decision': 'correct', 'reviewer': 'owner', 'reason': 'checked', 'corrected_brief': bad, 'calculation_reviews': [row]}
    assert client.post(url + '/review', json=payload).status_code == 400
    for mutate in ('missing', 'trace', 'quote', 'extra', 'list_id', 'dict_id'):
        tampered = copy.deepcopy(correction)
        if mutate == 'missing':
            tampered['answers'] = []
        elif mutate == 'trace':
            tampered['answers'][0]['calculation']['result'] = '999'
        elif mutate == 'quote':
            tampered['answers'][0]['source_excerpts'][0]['quote'] = 'fabricated'
        elif mutate in ('list_id', 'dict_id'):
            tampered['answers'][0]['question_id'] = [] if mutate == 'list_id' else {}
        else:
            tampered['answers'][0]['calculation']['extra'] = 'fabricated'
        payload['corrected_brief'] = tampered
        assert client.post(url + '/review', json=payload).status_code == 400
    payload['corrected_brief'] = correction
    original_bindings_valid = inv._bindings_valid
    checks = []
    async def invalidate_during_save(db, refs, evidence_scope):
        checks.append(1)
        if len(checks) == 2:
            await db.execute("UPDATE source_collections SET scan_status='failed' WHERE id=?", (cid,))
        return await original_bindings_valid(db, refs, evidence_scope)
    monkeypatch.setattr(inv, '_bindings_valid', invalidate_during_save)
    assert client.post(url + '/review', json=payload).status_code == 404
    assert len(checks) == 2
    monkeypatch.setattr(inv, '_bindings_valid', original_bindings_valid)
    assert client.get(url, params=scope).json()['status'] == 'draft'
    reviewed = client.post(url + '/review', json=payload)
    assert reviewed.status_code == 200, reviewed.text
    saved = reviewed.json()
    assert saved['brief'] == item['brief'] and saved['provenance'] == item['provenance']
    assert saved['review']['calculation_reviews'] == preview.json()['calculation_reviews']
    assert saved['review']['corrected_brief']['answers'][0] == answer
    handoff = client.get(url + '/export', params=scope).json()['markdown']
    assert 'Reviewed calculation choices' in handoff and 'Original machine calculations' in handoff
    for detail in ('Algorithm:', 'Formula:', 'Evaluated expression:', 'Normalized before', 'Limit:', 'Reviewed order'):
        assert detail in handoff
    assert len(calls) == 1
    (folder / 'release.md').write_text('# Release\nChanged.')
    asyncio.run(scan_collection('w', cid))
    assert client.post(url + '/calculation-preview', json={**scope, 'calculation_reviews': [row]}).status_code == 404


def source_item(text, operation='difference', version='v3'):
    from potluck_local.services import investigation_answers, investigation_calculations
    evidence = [{'kind': 'document', 'citation_id': 'C1', 'content': text, 'presented_content': text}]
    snippets = investigation_answers.snippets_from_evidence(evidence)
    quantities = investigation_calculations.quantity_catalog(snippets, source_context={'C1': text}, version=version)
    return {'status': 'draft', 'evidence': evidence, 'provenance': {
        'template_version': 'investigation-v9', 'snippet_catalog': snippets, 'quantity_catalog': quantities,
        'operation_plan': [{'id': 'R1', 'operation': operation, 'request': 'Change?'}],
        **({'quantity_parser_version': version} if version != 'v2' else {})}}


def selection(**kwargs):
    return {'request_id': 'R1', 'status': 'calculate', 'operand_ids': ['N1', 'N2'],
            'snippet_ids': [], 'reason': 'Owner choice', **kwargs}


def test_source_selection_and_legacy_catalog():
    import pytest
    from potluck_local.services import investigation_calculation_review as review
    text = 'Before 10 invoice errors. After 20 invoice errors. The signed contract governs.'
    item = source_item(text)
    row = selection(snippet_ids=['S1', 'S2', 'S3'])
    result = review.build(item, [row])
    assert [e['quote'] for e in result['answers'][0]['source_excerpts']] == [s['quote'] for s in item['provenance']['snippet_catalog']]
    assert 'authority is not verified' in result['answers'][0]['calculation']['source_selection_assessment']
    legacy_text = 'Before 10 ms. After 20 ms. There were 10 invoice errors.'
    legacy = source_item(legacy_text, version='v2')
    newer = source_item(legacy_text, version='v3')
    assert legacy['provenance']['quantity_catalog'] != newer['provenance']['quantity_catalog']
    result = review.build(legacy, [selection()])
    assert result['answers'][0]['calculation']['algorithm_version'] == 'cited-calculations-v2'
    assert result['answers'][0]['calculation']['result'] == '0.01'
    result = review.build(legacy, [selection(status='conflicting', operand_ids=[], snippet_ids=['S1', 'S2', 'S3'])])
    assert result['answers'][0]['calculation']['assessment'] == 'reviewer assessment'
    for rows in ([selection(request_id='R9')], [row, row]):
        with pytest.raises(ValueError):
            review.build(item, rows)
    tampered = copy.deepcopy(item)
    tampered['provenance']['quantity_catalog'][0]['value'] = '999'
    with pytest.raises(ValueError):
        review.build(tampered, [row])


def test_zero_and_incompatible_calculations():
    import pytest
    from potluck_local.services import investigation_calculation_review as review
    for operation, text, expected in [('percent_change', 'Before 0 ms. After 10 ms.', 'The baseline is zero.'),
                                      ('percentage', 'Part 10 ms. Total 0 ms.', 'The total is zero.')]:
        trace = review.build(source_item(text, operation), [selection()])['answers'][0]['calculation']
        assert trace['reason'] == expected and trace['limitation_kind'] == 'zero_denominator'
    with pytest.raises(ValueError):
        review.build(source_item('Before 0 ms. After 10 users.', 'percent_change'), [selection()])


def test_preview_rechecks_draft_state(monkeypatch):
    import pytest
    from potluck_local.services import investigation_calculation_review
    from potluck_local.services.evidence_lookup import EvidenceScope
    draft = source_item('Before 1 ms. After 2 ms.')
    draft['brief'] = {}
    calls = []
    async def get(*args):
        calls.append(1)
        result = copy.deepcopy(draft)
        if len(calls) == 2:
            result['status'] = 'reviewed'
        return result
    monkeypatch.setattr(inv, 'get_investigation', get)
    with pytest.raises(ValueError, match='changed'):
        asyncio.run(inv.calculation_preview(EvidenceScope('w', ('c',)), 'i', [selection()]))
    assert len(calls) == 2
