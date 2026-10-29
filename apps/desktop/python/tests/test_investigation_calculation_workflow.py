import asyncio
import json
from types import SimpleNamespace

import pytest

from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.source_collections import register_collection, scan_collection
from potluck_local.routers.investigations import export


def test_v8_planning_correction_export_and_source_invalidation(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    calls = []

    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            calls.append((payload, kwargs))
            if 'operations' not in payload:
                assert set(payload) == {'issue', 'questions'}
                data = {'operations': [{'question_id': 'Q1', 'request': 'How did latency change?', 'operation': 'difference'}]}
            else:
                assert kwargs['bounded_generation'] and kwargs['max_tokens'] == 2000
                assert payload['quantities'][0]['value'] == '100'
                assert payload['quantities'][0]['unit_source_text'] == 'ms'
                data = {'R1': {'status': 'ready', 'operand_ids': ['N1', 'N2'], 'snippet_ids': [], 'reason': 'Source quantities selected.'}}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}

    monkeypatch.setattr(inv, 'get_runtime', Runtime)

    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'
        folder.mkdir()
        document = folder / 'release.md'
        document.write_text('# Release\nLatency changed from 100 to 200 ms.')
        cid = await register_collection('w', 'docs', folder)
        await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        item = await inv.create_investigation(scope, 'How did latency change?', 'release', 'model',
                                              template_version='investigation-v8')
        assert len(calls) == 2 and calls[0][1]['max_tokens'] == 800
        assert item['brief']['answers'][0]['calculation']['result'] == '0.1'
        with pytest.raises(LookupError):
            await inv.get_investigation(EvidenceScope('other', (cid,)), item['id'])
        from fastapi import HTTPException
        from potluck_local.routers.investigations import review, ReviewRequest
        for bad_id in ({}, []):
            malformed = json.loads(json.dumps(item['brief']))
            malformed['answers'][0]['question_id'] = bad_id
            with pytest.raises(inv.BriefValidationError):
                await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'checked', malformed)
            body = ReviewRequest(workspace='w', collection_ids=[cid], decision='correct',
                                 reviewer='owner', reason='checked', corrected_brief=malformed)
            with pytest.raises(HTTPException) as error:
                await review(item['id'], body)
            assert error.value.status_code == 400
        correction = json.loads(json.dumps(item['brief']))
        correction['answers'][0]['calculation']['inputs'][0]['unit_source_text'] = 'hours'
        with pytest.raises(inv.BriefValidationError):
            await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'checked', correction)
        correction = json.loads(json.dumps(item['brief']))
        correction['answers'][0]['calculation']['result'] = '999'
        with pytest.raises(inv.BriefValidationError):
            await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'checked', correction)
        correction = json.loads(json.dumps(item['brief']))
        correction['answers'][0]['text'] = 'Owner interpretation.'
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'checked', correction)
        assert 'calculation' not in reviewed['review']['corrected_brief']['answers'][0]
        handoff = (await export(item['id'], 'w', [cid]))['markdown']
        assert 'Owner interpretation' in handoff and 'Original machine calculations' in handoff
        assert 'Formula:' in handoff and 'Rounding:' in handoff and 'Input N1' in handoff
        assert 'Original request:' in handoff and 'cited\\-calculations\\-v1' in handoff
        assert 'Evaluated expression:' in handoff and 'Normalized before' in handoff
        assert 'Shared unit: ms' in handoff and 'snippet offsets' in handoff
        document.write_text('# Release\nLatency changed.')
        await scan_collection('w', cid)
        with pytest.raises(LookupError):
            await inv.get_investigation(scope, item['id'])
    asyncio.run(run())


def test_v8_explicit_plan_and_atomic_invalid_response(tmp_path, monkeypatch):
    import aiosqlite
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    calls = []

    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            calls.append(json.loads(messages[1]['content']))
            return {'choices': [{'message': {'content': json.dumps({'R1': {
                'status': 'ready', 'operand_ids': ['N99'], 'snippet_ids': [], 'reason': 'Source quantities selected.'}})}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)

    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'
        folder.mkdir()
        (folder / 'release.md').write_text('# Release\nLatency was 100 ms.')
        cid = await register_collection('w', 'docs', folder)
        await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        plan = [{'question_id': 'Q1', 'request': 'Difference', 'operation': 'difference'}]
        with pytest.raises(ValueError):
            await inv.create_investigation(scope, 'i' * 4000, 'release', 'model',
                                           template_version='investigation-v8', operation_plan=plan)
        assert len(calls) == 1 and len(calls[0]['issue']) == 4000
        async with aiosqlite.connect(tmp_path / 'db.sqlite') as db:
            await inv.ensure_schema(db)
            async with db.execute('SELECT count(*) FROM investigations') as cursor:
                assert (await cursor.fetchone())[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize('source,forbidden', [
    ('x ' * 2999 + '125 ms. SECRET_SUFFIX', {'12', '125'}),
    ('x' * 498 + ':125 ms. SECRET_SUFFIX', {'1', '25', '125'}),
])
def test_v8_does_not_select_partial_quantity_at_presentation_boundary(tmp_path, monkeypatch, source, forbidden):
    from potluck_local.services.investigation_calculations import quantity_catalog
    assert any(q['value'] == '125' for q in quantity_catalog(
        [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': source}]))
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    original_lookup = inv.lookup_evidence

    async def lookup(scope, query, limit):
        result = await original_lookup(scope, query, limit)
        result['source_chunks'][0]['content'] = source
        return result
    monkeypatch.setattr(inv, 'lookup_evidence', lookup)

    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            assert not (forbidden & {q['source_text'].split()[0] for q in payload['quantities']})
            if len(source) > 6000:
                assert 'SECRET_SUFFIX' not in messages[1]['content']
            data = {'R1': {'status': 'unknown', 'operand_ids': [], 'snippet_ids': [],
                           'reason': 'No complete operands are presented.'}}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)

    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'
        folder.mkdir()
        (folder / 'release.md').write_text('# Release\nRecorded latency.')
        cid = await register_collection('w', 'docs', folder)
        await scan_collection('w', cid)
        item = await inv.create_investigation(
            EvidenceScope('w', (cid,)), 'Calculate change', 'release', 'model',
            template_version='investigation-v8',
            operation_plan=[{'question_id': 'Q1', 'request': 'Change', 'operation': 'difference'}])
        assert item['brief']['answers'][0]['status'] == 'unknown'
    asyncio.run(run())
