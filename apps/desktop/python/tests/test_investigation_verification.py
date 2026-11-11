import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import aiosqlite
import pytest

from potluck_local.services import investigations as inv
from potluck_local.services import investigation_verification as service
from potluck_local.services import investigation_draft_verification as contract
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.memory_db import init_memory_db, get_memory_db_path
from potluck_local.services.source_collections import register_collection, scan_collection


class Runtime:
    def __init__(self):
        self.calls = []; self.mode = 'clear'; self.hook = None; self.sha = 'a'*64
    def current_model_id(self): return 'model'
    def current_model_sha256(self): return self.sha
    async def validate_investigation_profile(self, *args, **kwargs): pass
    async def measure_investigation_prompt(self, messages, **kwargs):
        return {'input_tokens': 7000 if self.mode == 'capacity' else 1000, 'context_tokens': 8192}
    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs)); data = json.loads(messages[1]['content'])
        if data.get('version') not in (contract.VERSION, 'draft-verification-v2', 'draft-verification-v3'):
            ids = [s['snippet_id'] for s in data['snippets'] if 'Managers' in s['text']]
            result = {'R1': {'groups': [{'focus': 'Receipt requirement', 'status': 'supported',
                'snippet_ids': ids, 'conditions': [], 'exceptions': [], 'distinctions': [],
                'conclusion': 'Receipts are required.', 'unknown_reason': ''}], 'more': False}}
        else:
            assert kwargs['expected_context_tokens'] == 8192
            assert kwargs['expected_model'] == 'model' and kwargs['expected_sha256'] == 'a'*64
            assert kwargs['bounded_generation'] and kwargs['max_tokens'] == 2000
            if self.hook: await self.hook()
            if self.mode == 'timeout': raise asyncio.TimeoutError()
            if self.mode == 'transport': raise Exception('Do not persist private transport details')
            if self.mode == 'model_changed': self.sha = 'b'*64
            result = {'source_checks': {x['id']: {'verdict': 'adequate', 'reason': 'Complete.', 'omitted_quote': ''} for x in data['source_checks']},
                      'claim_checks': {x['id']: {'verdict': 'supported', 'reason': 'Supported.'} for x in data['claims']},
                      'group_checks': {x['id']: {'verdict': 'clear', 'reason': 'Complete.'} for x in data['groups']}}
            if self.mode == 'omission':
                result['source_checks']['Q1'] = {'verdict': 'omission', 'reason': 'Manager discretion is missing.',
                                               'omitted_quote': 'Managers may deny reimbursement.'}
            if self.mode == 'malformed': result['claim_checks'] = {}
            if data['version'] == 'draft-verification-v2':
                result = {'draft_omissions': result['source_checks'], 'claim_support': result['claim_checks'],
                          'draft_wording': result['group_checks']}
                for key, rows in result.items():
                    for value in rows.values():
                        if value['verdict'] in ('adequate', 'supported', 'clear'): value['reason'] = ''
                        if value['verdict'] == 'adequate': value['verdict'] = 'no_omission'
                        if value['verdict'] == 'omission': value['verdict'] = 'omitted_from_draft'
            if data['version'] == 'draft-verification-v3':
                from test_investigation_verification_v3 import clear
                result = clear(data)
                if self.mode == 'omission':
                    unit = next(c for c in data['clauses'] if 'Managers' in c['text'])
                    result['clause_coverage'][unit['id']].update(verdict='material_omission', reason='Manager discretion is missing.')
                if self.mode == 'malformed': result['claim_support'] = {}
        return {'choices': [{'message': {'content': json.dumps(result)},
                             'finish_reason': 'length' if self.mode == 'truncated' else 'stop'}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path/'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    runtime = Runtime(); monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
    async def create():
        await init_memory_db(); folder = tmp_path/'docs'; folder.mkdir()
        path = folder/'policy.md'; path.write_text('# Expenses\n\nReceipts are required. Managers may deny reimbursement.\n')
        cid = await register_collection('w', 'docs', folder); await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        item = await inv.create_investigation(scope, 'What is required for reimbursement?', 'reimbursement', 'model',
            template_version='investigation-v21', reviewed_operations=[{'request': 'What is required for reimbursement?', 'operation': 'answer'}],
            generation_profile=service.PROFILE, prompt_encoding='compact-calculation-v1',
            reliability_mode='bounded-evidence-v1', unknown_mode='cited-unknown-v4', presentation_mode='cited-components-v1')
        runtime.calls.clear()
        return runtime, scope, item, path
    return create


def test_check_preserves_original_and_survives_owner_correction_and_export(setup):
    async def run():
        runtime, scope, original, _ = await setup(); runtime.mode = 'omission'
        checked = await service.verify_investigation(scope, original['id'])
        assert len(runtime.calls) == 1 and checked['status'] == 'draft' and checked['review'] is None
        assert checked['brief'] == original['brief'] and checked['evidence'] == original['evidence']
        reports = service.validate_reports(checked); assert reports[0]['result']['status'] == 'needs_review'
        assert {k: v for k, v in checked['provenance'].items() if k != 'draft_verifications'} == original['provenance']
        correction = deepcopy(original['brief']); correction['answers'][0]['text'] = 'Receipts are required. Management may deny reimbursement.'
        reviewed = await inv.review_investigation(scope, original['id'], 'correct', 'owner', 'Fixture only.', correction)
        assert reviewed['provenance'] == checked['provenance'] and reviewed['brief'] == original['brief']
        from potluck_local.routers.investigations import export
        markdown = (await export(original['id'], 'w', list(scope.collection_ids)))['markdown']
        assert 'Local draft checks (unverified)' in markdown and 'Managers may deny reimbursement' in markdown
        assert 'do not approve the answer or evaluate owner corrections' in markdown
        with pytest.raises(ValueError, match='unreviewed'): await service.verify_investigation(scope, original['id'])
    asyncio.run(run())


@pytest.mark.parametrize('mode,reason,calls', [('capacity','capacity',0),('timeout','timeout',1),
    ('malformed','invalid_output',1),('truncated','invalid_output',1),('transport','inference_failed',1),
    ('model_changed','model_changed',1)])
def test_failure_is_not_checked_without_rewriting_or_retrying(setup, mode, reason, calls):
    async def run():
        runtime, scope, original, _ = await setup(); runtime.mode = mode
        checked = await service.verify_investigation(scope, original['id']); report = service.validate_reports(checked)[0]
        assert report['result'] == {'version': contract.VERSION, 'status': 'not_checked', 'reason': reason, 'scope': contract.SCOPE}
        assert report['settings']['generation_calls'] == len(runtime.calls) == calls
        assert checked['brief'] == original['brief'] and checked['status'] == 'draft'
        assert 'private transport' not in json.dumps(checked)
    asyncio.run(run())


@pytest.mark.parametrize('race', ['review', 'rescan', 'verification'])
def test_changes_during_generation_cannot_be_overwritten(setup, race):
    async def run():
        runtime, scope, original, path = await setup()
        async def change():
            runtime.hook = None
            if race == 'review':
                await inv.review_investigation(scope, original['id'], 'accept', 'owner', 'Fixture review.')
            elif race == 'rescan':
                path.write_text('# Expenses\n\nReceipts are no longer required.\n')
                await scan_collection('w', scope.collection_ids[0])
            else: await service.verify_investigation(scope, original['id'])
        runtime.hook = change
        with pytest.raises((ValueError, LookupError)): await service.verify_investigation(scope, original['id'])
        async with aiosqlite.connect(get_memory_db_path()) as db:
            row = await (await db.execute('SELECT status,provenance FROM investigations WHERE id=?', (original['id'],))).fetchone()
        assert len(json.loads(row[1] or '{}').get('draft_verifications', [])) == (1 if race == 'verification' else 0)
        if race == 'review': assert row[0] == 'reviewed'
        if race == 'rescan': assert row[1] is None
    asyncio.run(run())


def test_attempt_limit_and_invalid_scope_prevent_generation(setup):
    async def run():
        runtime, scope, item, _ = await setup()
        with pytest.raises(LookupError): await service.verify_investigation(EvidenceScope('other', scope.collection_ids), item['id'])
        with pytest.raises(ValueError): await service.verify_investigation(scope, item['id'], seed=True)
        assert runtime.calls == []
        for _ in range(3): await service.verify_investigation(scope, item['id'])
        with pytest.raises(ValueError, match='limit'): await service.verify_investigation(scope, item['id'])
        assert len(runtime.calls) == 3
    asyncio.run(run())


@pytest.mark.parametrize('change', ['id', 'input', 'decision', 'result_type', 'budget', 'settings', 'timestamp'])
def test_invalid_saved_assessment_rejects(setup, change):
    async def run():
        _, scope, item, _ = await setup(); checked = await service.verify_investigation(scope, item['id'])
        report = checked['provenance']['draft_verifications'][0]
        if change == 'id': report['id'] = []
        if change == 'input': report['input']['groups'][0]['focus'] = 'Different'
        if change == 'decision':
            report['result']['status'] = 'needs_review'; report['result_sha256'] = contract.digest(report['result'])
        if change == 'result_type': report['result'] = None
        if change == 'budget': report['input_budget']['input_tokens'] = 8000
        if change == 'settings': report['settings']['generation_calls'] = False
        if change == 'timestamp': report['created_at'] = 1
        with pytest.raises(ValueError): service.validate_reports(checked)
    asyncio.run(run())


def test_http_scope_seed_and_original_draft_contract(setup):
    async def run():
        import httpx
        from fastapi import FastAPI
        from potluck_local.routers.investigations import router
        runtime, scope, item, _ = await setup(); app = FastAPI(); app.include_router(router)
        payload = {'workspace': scope.workspace, 'collection_ids': list(scope.collection_ids)}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            url = '/company-brain/investigations/' + item['id'] + '/draft-verification'
            assert (await client.post(url, json={**payload, 'seed': True})).status_code == 422
            assert (await client.post(url, json={**payload, 'workspace': 'other'})).status_code == 404
            result = await client.post(url, json=payload)
            assert result.status_code == 200, result.text
            assert result.json()['status'] == 'draft' and len(runtime.calls) == 1
            assert result.json()['provenance']['draft_verifications'][0]['result']['status'] == 'no_issue_found'
    asyncio.run(run())


def test_cancelled_run_does_not_save_a_completed_assessment(setup):
    async def run():
        runtime, scope, item, _ = await setup()
        async def cancel(): raise asyncio.CancelledError()
        runtime.hook = cancel
        with pytest.raises(asyncio.CancelledError): await service.verify_investigation(scope, item['id'])
        assert (await inv.get_investigation(scope, item['id']))['provenance'] == item['provenance']
    asyncio.run(run())


def test_simultaneous_checks_cannot_take_the_same_last_report_slot(setup):
    async def run():
        runtime, scope, item, _ = await setup()
        for _ in range(2): await service.verify_investigation(scope, item['id'])
        both_started = asyncio.Event(); waiting = 0
        async def barrier():
            nonlocal waiting
            waiting += 1
            if waiting == 2: both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5)
        runtime.hook = barrier
        results = await asyncio.gather(service.verify_investigation(scope, item['id']),
                                       service.verify_investigation(scope, item['id']), return_exceptions=True)
        assert sum(isinstance(r, dict) for r in results) == 1
        assert sum(isinstance(r, ValueError) for r in results) == 1
        final = await inv.get_investigation(scope, item['id'])
        reports = service.validate_reports(final)
        assert len(reports) == len({r['id'] for r in reports}) == 3
        assert final['brief'] == item['brief'] and final['status'] == 'draft'
    asyncio.run(run())
