import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.source_collections import register_collection, scan_collection


def test_plan_confirm_create_review_export_and_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('POTLUCK_SIDECAR_TOKEN', raising=False)
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    calls = []

    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            calls.append(payload)
            if set(payload) == {'issue'}:
                data = {'questions': ['How did latency change?']}
            elif 'questions' in payload:
                data = {'Q1': 'difference'}
            else:
                assert 'extraction_notices' in payload
                data = {'R1': {'status': 'ready', 'operand_ids': ['N1', 'N2'],
                               'snippet_ids': [], 'reason': 'Before and after selected.'}}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    asyncio.run(init_memory_db())
    folder = tmp_path / 'docs'
    folder.mkdir()
    document = folder / 'release.md'
    document.write_text('# Release\nLatency changed from 100 to 200 ms. Weight was 15 kg.')
    cid = asyncio.run(register_collection('w', 'docs', folder))
    asyncio.run(scan_collection('w', cid))
    scope = {'workspace': 'w', 'collection_ids': [cid]}
    client = TestClient(create_app(), client=('127.0.0.1', 44444))
    plan = client.post('/company-brain/investigations/plan', json={**scope, 'issue': 'How did latency change?', 'model_slug': 'model'})
    assert plan.status_code == 200, plan.text
    rows = plan.json()['operations']
    assert rows == [{'request': 'How did latency change?', 'operation': 'difference'}]
    assert len(calls) == 2 and all('snippets' not in call for call in calls)
    assert client.get('/company-brain/investigations', params=scope).json()['investigations'] == []
    base = {**scope, 'issue': 'How did latency change?', 'query': 'release', 'model_slug': 'model'}
    for fields in ({'reviewed_operations': rows}, {'plan_confirmed': True},
                   {'reviewed_operations': rows, 'plan_confirmed': True, 'answer_each_question': True},
                   {'reviewed_operations': rows, 'plan_confirmed': True, 'questions': ['x']},
                   {'reviewed_operations': rows * 2, 'plan_confirmed': True}):
        assert client.post('/company-brain/investigations', json={**base, **fields}).status_code == 400
    result = client.post('/company-brain/investigations', json={**base, 'reviewed_operations': rows, 'plan_confirmed': True})
    assert result.status_code == 200, result.text
    item = result.json()
    assert item['provenance']['operation_plan_origin'] == 'confirmed'
    fixture_item = asyncio.run(inv.create_investigation(EvidenceScope('w', (cid,)),
        'How did latency change?', 'release', 'model', template_version='investigation-v9',
        reviewed_operations=rows, reviewed_plan_origin='fixture_supplied'))
    assert fixture_item['provenance']['operation_plan_origin'] == 'fixture_supplied'
    assert client.post('/company-brain/investigations', json={**base, 'reviewed_operations': rows,
        'plan_confirmed': True, 'reviewed_plan_origin': 'fixture_supplied'}).status_code == 422
    assert item['provenance']['extraction_notices']
    assert item['brief']['answers'][0]['calculation']['result'] == '0.1'
    bad = json.loads(json.dumps(item['brief']))
    bad['answers'][0]['calculation']['result'] = '999'
    url = '/company-brain/investigations/' + item['id']
    assert client.post(url + '/review', json={**scope, 'decision': 'correct', 'reviewer': 'owner',
                                              'reason': 'checked', 'corrected_brief': bad}).status_code == 400
    correction = json.loads(json.dumps(item['brief']))
    correction['answers'][0]['text'] = 'Owner checked.'
    reviewed = client.post(url + '/review', json={**scope, 'decision': 'correct', 'reviewer': 'owner',
                                                   'reason': 'checked', 'corrected_brief': correction})
    assert reviewed.status_code == 200, reviewed.text
    assert 'calculation' not in reviewed.json()['review']['corrected_brief']['answers'][0]
    handoff = client.get(url + '/export', params=scope).json()['markdown']
    assert 'Quantity extraction notices' in handoff and 'Original machine calculations' in handoff
    assert client.get(url, params={**scope, 'workspace': 'wrong'}).status_code == 404
    document.write_text('# Release\nChanged.')
    asyncio.run(scan_collection('w', cid))
    assert client.get(url, params=scope).status_code == 404


@pytest.mark.parametrize('failure', ['model_change', 'scope_change', 'bad_plan'])
def test_plan_fails_atomically_on_race_or_invalid_model_output(tmp_path, monkeypatch, failure):
    import aiosqlite
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    state = {'model': 'model', 'calls': 0}
    class Runtime:
        def current_model_id(self): return state['model']
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            state['calls'] += 1
            if failure == 'model_change':
                state['model'] = 'other'
            if failure == 'scope_change' and state['calls'] == 2:
                async with aiosqlite.connect(tmp_path / 'db.sqlite') as db:
                    await db.execute("UPDATE source_collections SET scan_status='failed'")
                    await db.commit()
            data = ({'questions': ['Question?']} if state['calls'] == 1 else
                    {'Q2' if failure == 'bad_plan' else 'Q1': 'answer'})
            return {'choices': [{'message': {'content': json.dumps(data)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'
        folder.mkdir()
        (folder / 'release.md').write_text('# Release\nContent.')
        cid = await register_collection('w', 'docs', folder)
        await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        message = {'model_change': 'Model changed during inference',
                   'scope_change': 'Evidence changed during investigation',
                   'bad_plan': 'Invalid model format'}[failure]
        with pytest.raises(ValueError, match=message):
            await inv.suggest_investigation_plan(scope, 'Question?', 'model')
        assert state['calls'] == (1 if failure == 'model_change' else 2)
        async with aiosqlite.connect(tmp_path / 'db.sqlite') as db:
            await inv.ensure_schema(db)
            async with db.execute('SELECT count(*) FROM investigations') as cursor:
                assert (await cursor.fetchone())[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['model_change', 'evidence_change'])
def test_v9_create_race_does_not_save_investigation(tmp_path, monkeypatch, failure):
    import aiosqlite

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    state = {'model': 'model', 'calls': 0, 'collection': None}
    folder = tmp_path / 'docs'
    folder.mkdir()
    document = folder / 'release.md'
    document.write_text('# Release\nLatency changed from 100 to 200 ms.')

    class Runtime:
        def current_model_id(self): return state['model']
        def current_model_sha256(self): return 'a' * 64

        async def chat(self, messages, **kwargs):
            state['calls'] += 1
            payload = json.loads(messages[1]['content'])
            assert payload['operations'][0]['operation'] == 'difference'
            assert [item['value'] for item in payload['quantities']] == ['100', '200']
            if failure == 'model_change':
                state['model'] = 'other'
            else:
                document.write_text('# Release\nLatency changed from 200 to 300 ms.')
                await scan_collection('w', state['collection'])
            data = {'R1': {'status': 'ready', 'operand_ids': ['N1', 'N2'],
                           'snippet_ids': [], 'reason': 'Before and after selected.'}}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}

    monkeypatch.setattr(inv, 'get_runtime', Runtime)

    async def run():
        await init_memory_db()
        cid = await register_collection('w', 'docs', folder)
        state['collection'] = cid
        await scan_collection('w', cid)
        message = ('Model changed during inference' if failure == 'model_change'
                   else 'Evidence changed during investigation')
        with pytest.raises(ValueError, match=message):
            await inv.create_investigation(
                EvidenceScope('w', (cid,)), 'How did latency change?', 'release', 'model',
                template_version='investigation-v9',
                reviewed_operations=[{'request': 'How did latency change?', 'operation': 'difference'}])
        assert state['calls'] == 1
        async with aiosqlite.connect(tmp_path / 'db.sqlite') as db:
            await inv.ensure_schema(db)
            async with db.execute('SELECT count(*) FROM investigations') as cursor:
                assert (await cursor.fetchone())[0] == 0

    asyncio.run(run())
