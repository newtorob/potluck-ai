import asyncio
import sqlite3

import pytest
from fastapi import HTTPException

from potluck_local.routers import company_brain_pilot as pilot


def test_pilot_lifecycle_and_stale_citations(tmp_path, monkeypatch):
    db = tmp_path / 'db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(db))
    monkeypatch.setattr(pilot, '_privacy', lambda: None)
    folder = tmp_path / 'docs'
    folder.mkdir()
    source = folder / 'one.md'
    source.write_text('# Policy\n\nReview the evidence carefully.')

    async def run():
        reg = pilot.Register(workspace='w', name='docs', folder=str(folder), scan_confirmed=True)
        cid = (await pilot.register(reg))['collection_id']
        with pytest.raises(HTTPException):
            await pilot.register(reg)
        with pytest.raises(HTTPException):
            await pilot.evidence(pilot.Evidence(workspace='w', collection_ids=[cid], query='evidence'))
        await pilot.scan(cid, pilot.Scan(workspace='w', scan_confirmed=True))
        scope = dict(workspace='w', collection_ids=[cid])
        result = await pilot.evidence(pilot.Evidence(**scope, query='evidence'))
        citation = result['source_chunks'][0]['citation_id']
        proposal = pilot.Proposal(**scope, claim_key='policy', content='Review evidence.', citation_ids=[citation])
        first = (await pilot.propose(proposal))['claim_id']
        listed = await pilot.claims(pilot.Claims(**scope))
        assert listed['claims'][0]['sources'][0]['content'].endswith('Review the evidence carefully.')
        with pytest.raises(HTTPException):
            await pilot.approve(first, pilot.Review(**scope, reviewer=' ', reason='checked'))
        await pilot.approve(first, pilot.Review(**scope, reviewer='owner', reason='checked'))
        second = (await pilot.propose(proposal))['claim_id']
        await pilot.supersede(first, pilot.Supersede(**scope, replacement_id=second, reviewer='owner', reason='updated'))
        assert (await pilot.claims(pilot.Claims(**scope, status='approved')))['claims'][0]['id'] == second
        source.write_text('# Changed\n\nNew source.')
        await pilot.scan(cid, pilot.Scan(workspace='w', scan_confirmed=True))
        with pytest.raises(HTTPException):
            await pilot.propose(proposal)
        with pytest.raises(HTTPException):
            await pilot.approve(second, pilot.Review(**scope, reviewer='owner', reason='checked'))
        with sqlite3.connect(db) as connection:
            assert connection.execute("select count(*) from reviewed_claims where status='approved'").fetchone()[0] == 0
    asyncio.run(run())


def test_ask_requires_confirmation_and_uses_only_fixed_options(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db'))
    monkeypatch.setattr(pilot, '_privacy', lambda: None)
    folder = tmp_path / 'docs'; folder.mkdir()
    (folder / 'x.md').write_text('# Data\n\nInformation')
    calls = []
    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return {'id': 'result'}
    monkeypatch.setattr(pilot, 'create_investigation', fake)
    async def run():
        cid = (await pilot.register(pilot.Register(workspace='w', name='x', folder=str(folder), scan_confirmed=True)))['collection_id']
        await pilot.scan(cid, pilot.Scan(workspace='w', scan_confirmed=True))
        params = dict(workspace='w', collection_ids=[cid], question='What happened?', model_slug='model')
        with pytest.raises(HTTPException):
            await pilot.ask(pilot.Ask(**params, pilot_confirmed=False))
        assert not calls
        assert await pilot.ask(pilot.Ask(**params, pilot_confirmed=True)) == {'id': 'result'}
        kwargs = calls[0][1]
        assert kwargs['evidence_selection'] == 'reviewed-collection-v5'
        assert kwargs['presentation_mode'] == 'reviewed-claim-selection-v2'
        assert kwargs['reviewed_operations'] == [{'request': params['question'], 'operation': 'answer'}]
        assert kwargs['generation_profile'] == 'qwen3.8-27b-native-non-thinking'
        with pytest.raises(HTTPException):
            await pilot.ask(pilot.Ask(**{**params, 'collection_ids': [cid, 'missing']}, pilot_confirmed=True))
        assert len(calls) == 1
        await pilot.ask(pilot.Ask(**params, pilot_confirmed=True, focused_passages=True))
        assert len(calls) == 2 and calls[-1][1]['presentation_mode'] == 'reviewed-claim-selection-v4'
    asyncio.run(run())


def test_http_auth_privacy_and_validation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from potluck_local.app import create_app
    from potluck_local.services import investigations
    monkeypatch.setenv('POTLUCK_SIDECAR_TOKEN', 'pilot-secret')
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path / 'data'))
    folder = tmp_path / 'docs'; folder.mkdir()
    (folder / 'one.md').write_text('# Policy\n\nEvidence.')
    client = TestClient(create_app(), client=('127.0.0.1', 50000))
    payload = {'workspace': 'w', 'name': 'docs', 'folder': str(folder), 'scan_confirmed': True}
    path = '/company-brain/pilot/collections'
    assert client.post(path, json=payload).status_code == 401
    headers = {'Authorization': 'Bearer pilot-secret'}
    monkeypatch.setattr(investigations, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=False), memory=SimpleNamespace(enabled=True)))
    assert client.post(path, json=payload, headers=headers).status_code == 403
    monkeypatch.setattr(investigations, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    for invalid in ({'scan_confirmed': False}, {'folder': 'relative'}, {'name': ' docs '}):
        assert client.post(path, json={**payload, **invalid}, headers=headers).status_code == 400
    result = client.post(path, json=payload, headers=headers)
    assert result.status_code == 200
    cid = result.json()['collection_id']
    duplicate = client.post(path, json=payload, headers=headers)
    assert duplicate.status_code == 400 and 'already exists' in duplicate.json()['detail']
    scan = f'/company-brain/pilot/collections/{cid}/scan'
    assert client.post(scan, json={'workspace': 'w', 'scan_confirmed': False}, headers=headers).status_code == 400
    assert client.post(scan, json={'workspace': 'other', 'scan_confirmed': True}, headers=headers).status_code == 404
    assert client.post(scan, json={'workspace': 'w', 'scan_confirmed': True}, headers=headers).status_code == 200
    assert client.post('/company-brain/pilot/evidence', json={'workspace': 'w', 'collection_ids': [cid, cid], 'query': 'Evidence'}, headers=headers).status_code == 400
    assert client.post('/company-brain/pilot/evidence', json={'workspace': 'other', 'collection_ids': [cid], 'query': 'Evidence'}, headers=headers).status_code == 400
    result = client.post('/company-brain/pilot/evidence', json={'workspace': 'w', 'collection_ids': [cid], 'query': 'Evidence'}, headers=headers)
    citation = result.json()['source_chunks'][0]['citation_id']
    proposal = {'workspace': 'w', 'collection_ids': [cid], 'claim_key': 'k', 'content': 'Evidence', 'citation_ids': [citation + 'bad']}
    invalid = client.post('/company-brain/pilot/claims', json=proposal, headers=headers)
    assert invalid.status_code == 400 and 'citation' in invalid.json()['detail']
    proposal['citation_ids'] = [citation]
    first = client.post('/company-brain/pilot/claims', json=proposal, headers=headers).json()['claim_id']
    review = {'workspace': 'w', 'collection_ids': [cid], 'reviewer': 'owner', 'reason': 'checked'}
    assert client.post(f'/company-brain/pilot/claims/{first}/approve', json=review, headers=headers).status_code == 200
    second = client.post('/company-brain/pilot/claims', json=proposal, headers=headers).json()['claim_id']
    rejected = client.post(f'/company-brain/pilot/claims/{second}/approve', json=review, headers=headers)
    assert rejected.status_code == 400 and 'supersede' in rejected.json()['detail']
    (folder / 'one.md').write_text('# Changed\n\nNew evidence.')
    assert client.post(scan, json={'workspace': 'w', 'scan_confirmed': True}, headers=headers).status_code == 200
    assert client.post(f'/company-brain/pilot/claims/{second}/approve', json=review, headers=headers).status_code in (400, 404)


def test_claim_listing_limits_in_scope_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db'))
    monkeypatch.setattr(pilot, '_privacy', lambda: None)
    folder = tmp_path / 'docs'; folder.mkdir()
    (folder / 'one.md').write_text('# Policy\n\nEvidence.')
    async def run():
        cid = (await pilot.register(pilot.Register(workspace='w', name='docs', folder=str(folder), scan_confirmed=True)))['collection_id']
        await pilot.scan(cid, pilot.Scan(workspace='w', scan_confirmed=True))
        scope = {'workspace': 'w', 'collection_ids': [cid]}
        citation = (await pilot.evidence(pilot.Evidence(**scope, query='Evidence')))['source_chunks'][0]['citation_id']
        for i in range(101):
            await pilot.propose(pilot.Proposal(**scope, claim_key=f'k{i}', content='Evidence', citation_ids=[citation]))
        listing = await pilot.claims(pilot.Claims(**scope))
        assert len(listing['claims']) == 100 and listing['truncated'] is True
    asyncio.run(run())


def test_reviewed_ask_http_confirmation_scope_and_fixed_contract(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from potluck_local.app import create_app
    from potluck_local.services import investigation_request_parts

    monkeypatch.setenv('POTLUCK_SIDECAR_TOKEN', 'pilot-secret')
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db'))
    monkeypatch.setattr(pilot, '_privacy', lambda: None)
    folder = tmp_path / 'docs'
    folder.mkdir()
    (folder / 'one.md').write_text('# Policy\n\nApproval is required.')
    calls = []

    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return {'id': 'result'}

    monkeypatch.setattr(pilot, 'create_investigation', fake)
    with TestClient(create_app(), client=('127.0.0.1', 50000)) as client:
        headers = {'Authorization': 'Bearer pilot-secret'}
        root = '/company-brain/pilot'
        cid = client.post(root + '/collections', headers=headers, json={
            'workspace': 'w', 'name': 'docs', 'folder': str(folder), 'scan_confirmed': True,
        }).json()['collection_id']
        body = {'workspace': 'w', 'collection_ids': [cid], 'question': 'Which rule and expiry apply?',
                'model_slug': 'model', 'pilot_confirmed': True,
                'question_parts': ['Which rule applies?', 'Which exact expiry is stated?'],
                'question_parts_confirmed': True}
        path = root + '/ask-reviewed'
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers=headers).status_code == 400
        assert not calls
        assert client.post(root + f'/collections/{cid}/scan', headers=headers, json={
            'workspace': 'w', 'scan_confirmed': True,
        }).status_code == 200
        for invalid in (
            {'pilot_confirmed': False}, {'question_parts_confirmed': False},
            {'question_parts': ['Which rule?', ' WHICH  RULE? ']},
            {'question_parts': [' ']}, {'question_parts': ['x' * 501]},
            {'collection_ids': [cid, cid]}, {'workspace': 'other'},
        ):
            assert client.post(path, json={**body, **invalid}, headers=headers).status_code == 400
        for invalid in (
            {'question_parts': []}, {'question_parts': ['x'] * 7}, {'question_parts': [42]},
            {'question_parts_confirmed': 'true'}, {'focused_passages': True},
            {'presentation_mode': 'fixed-part-selection-v28'},
        ):
            assert client.post(path, json={**body, **invalid}, headers=headers).status_code == 422
        assert not calls
        assert client.post(path, json=body, headers=headers).json() == {'id': 'result'}
        assert len(calls) == 1
        args, options = calls[0]
        assert args[0].workspace == 'w' and args[0].collection_ids == (cid,)
        assert args[1:] == (body['question'], body['question'], 'model')
        assert options == {
            'template_version': 'investigation-v21',
            'generation_profile': 'qwen3.8-27b-native-non-thinking', 'seed': 7,
            'reviewed_operations': [{'request': body['question'], 'operation': 'answer'}],
            'prompt_encoding': 'compact-calculation-v1', 'reliability_mode': 'bounded-evidence-v1',
            'presentation_mode': 'fixed-part-selection-v32',
            'evidence_selection': 'reviewed-collection-v5',
            'question_part_plan': investigation_request_parts.prepare(
                body['question'], body['question_parts'], origin='reviewer_supplied'),
        }


def test_reviewed_pilot_uses_real_persistence_review_export_and_stale_guards(tmp_path, monkeypatch):
    import json
    from copy import deepcopy
    import test_investigation_fixed_part_answers_service as shared
    from test_investigation_single_review_v28 import wire
    from potluck_local.services import investigations as inv

    class Runtime(shared.FixedRuntime):
        async def chat(self, messages, **kwargs):
            self.calls.append(deepcopy((messages, kwargs)))
            return {'choices': [{'message': {'content': wire('supported', ['C1:rule'])},
                                 'finish_reason': 'stop'}]}

    async def run():
        path, scope, *_ = await shared.setup(tmp_path, monkeypatch)
        monkeypatch.setattr(pilot, '_privacy', lambda: None)
        runtime = Runtime()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        body = pilot.AskReviewed(
            workspace=scope.workspace, collection_ids=list(scope.collection_ids),
            question=shared.QUESTION, model_slug='model', pilot_confirmed=True,
            question_parts=['What is required for access?', 'What exact expiry is stated?'],
            question_parts_confirmed=True)
        item = await pilot.ask_reviewed(body)
        assert len(runtime.calls) == 2
        assert all(options['max_tokens'] == 256 for _, options in runtime.calls)
        assert item['provenance']['presentation_mode'] == 'fixed-part-selection-v32'
        assert item['provenance']['question_part_plan']['origin'] == 'reviewer_supplied'
        assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
        reviewed = await inv.review_investigation(
            scope, item['id'], 'reject', 'Synthetic tester', 'Transport check, not answer-quality credit')
        assert reviewed['brief'] == item['brief']
        exported = await shared.routes.export(item['id'], scope.workspace, list(scope.collection_ids))
        assert 'Transport check' in exported['markdown']
        with sqlite3.connect(path) as db:
            db.execute("UPDATE source_collections SET scan_status='pending'")
        with pytest.raises(HTTPException):
            await pilot.ask_reviewed(body)
        with pytest.raises((ValueError, LookupError)):
            await inv.get_investigation(scope, item['id'])
        assert len(runtime.calls) == 2
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT COUNT(*) FROM investigations').fetchone()[0] == 1
            contract = json.loads(db.execute('SELECT question_part_contract FROM investigations').fetchone()[0])
        assert contract['version'] == 'fixed-part-selection-v32'
    asyncio.run(run())
