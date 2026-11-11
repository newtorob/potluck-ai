"""Real investigation persistence with explicit source-plan authority."""
import asyncio
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import aiosqlite
import pytest

from potluck_local.services import investigation_reviewed_claim_selection_v15 as selection
from potluck_local.services import investigations as inv
from potluck_local.services import reviewed_claim_unit_plans as plans
from potluck_local.services import investigation_reviewed_unit_plans as bindings
from potluck_local.services import investigation_verification as verification
from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim
from potluck_local.services.reviewed_source_unit_plan import text_hash
from potluck_local.services.source_collections import register_collection, scan_collection
from potluck_local.routers import investigations as routes


BODY = '## Access\nApproval is required.\nSuspended members are excluded.\nUnrelated retention is 30 days.'
QUESTION = 'What is required for access and the exact expiry date?'


async def setup(tmp_path, monkeypatch, *, approve_plan=True):
    path = tmp_path / 'db.sqlite'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(path))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    await init_memory_db()
    folder = tmp_path / 'docs'
    folder.mkdir()
    (folder / 'policy.md').write_text(BODY)
    collection = await register_collection('w', 'docs', folder)
    await scan_collection('w', collection)
    scope = EvidenceScope('w', (collection,))
    result = await lookup_evidence(scope, 'Access', 10, retrieval_version='v2')
    refs = [{k: result['source_chunks'][0][k] for k in (
        'collection_id', 'document_id', 'revision_id', 'chunk_id')}]
    claim = await propose_claim('w', scope.collection_ids, 'Access', BODY, refs)
    await approve_claim('w', scope.collection_ids, claim, 'fixture source reviewer', 'Synthetic fixture approval')
    offsets = [0, BODY.index('Approval'), BODY.index('Suspended'), BODY.index('Unrelated'), len(BODY)]
    plan = {'source_sha256': text_hash(BODY), 'units': [
        {'id': name, 'ranges': [[offsets[i], offsets[i + 1]]], 'required_unit_ids': deps}
        for i, (name, deps) in enumerate([
            ('heading', []), ('rule', ['heading', 'exception']), ('exception', []), ('other', [])])]}
    proposal = await plans.propose_plan('w', scope.collection_ids, claim, plan)
    if approve_plan:
        await plans.approve_plan('w', scope.collection_ids, proposal, 'fixture plan reviewer', 'Synthetic explicit unit review')
    return path, scope, claim, plan, proposal


class Runtime:
    def __init__(self, status='supported', change=None, fallback=False, failure=None):
        self.calls = []
        self.measurements = []
        self.status = status
        self.change = change
        self.fallback = fallback
        self.failure = failure

    def current_model_id(self): return 'model'
    def current_model_sha256(self): return 'a' * 64
    async def validate_investigation_profile(self, *args, **kwargs): return None

    async def measure_investigation_prompt(self, messages, **kwargs):
        self.measurements.append(deepcopy((messages, kwargs)))
        return {'input_tokens': 120, 'context_tokens': 8192}

    async def chat(self, messages, **kwargs):
        self.calls.append(deepcopy((messages, kwargs)))
        call = len(self.calls)
        if self.change and call == 1:
            # A write through another connection proves no model-spanning lock.
            await asyncio.wait_for(self.change(), timeout=2)
        if self.failure == 'first' and call == 1:
            return {'choices': [{'message': {'content': '{}'}}]}
        if self.failure == 'second' and call == 2:
            return {'choices': [{'message': {'content': '{}'}}]}
        if call == 1:
            data = {'unit_ids': ['C1:whole' if self.fallback else 'C1:other']}
        else:
            data = {'review_unit_ids': ['C1:whole' if self.fallback else 'C1:rule'],
                    'missing_start_token_id': 'T8' if self.status == 'unknown' else '',
                    'missing_end_token_id': 'T10' if self.status == 'unknown' else '',
                    'conflicting': self.status == 'conflicting'}
        return {'choices': [{'message': {'content': json.dumps(data)}}]}


async def create(scope, question=QUESTION, mode=selection.VERSION):
    return await inv.create_investigation(scope, question, 'Access', 'model',
        template_version='investigation-v21',
        reviewed_operations=[{'request': question, 'operation': 'answer'}],
        evidence_selection='reviewed-current-v1', presentation_mode=mode,
        generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1',
        reliability_mode='bounded-evidence-v1')


@pytest.mark.parametrize('status', ['supported', 'conflicting', 'unknown'])
def test_save_get_correction_export_outcome_and_supersession(tmp_path, monkeypatch, status):
    async def run():
        path, scope, claim, plan, first = await setup(tmp_path, monkeypatch)
        runtime = Runtime(status)
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        draft = await create(scope)
        answer = draft['brief']['answers'][0]
        assert answer['status'] == status
        assert all(text in answer['text'] for text in ('Approval is required.', 'Suspended members are excluded.'))
        assert 'Unrelated retention' not in answer['text']
        assert 'unit_plan_contract' not in draft
        trace = draft['provenance']['reviewed_claim_selection']
        assert trace['review']['dropped_proposal_unit_ids'] == ['C1:other']
        assert [c[1]['max_tokens'] for c in runtime.calls] == [2000, 512]
        assert all(m[0] == c[0] for m, c in zip(runtime.measurements, runtime.calls))
        assert draft['provenance'][bindings.RECEIPTS_KEY]['claim:' + claim]['id'] == first
        assert (await inv.get_investigation(scope, draft['id']))['brief'] == draft['brief']
        exported = (await routes.export(draft['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        assert 'Suspended members are excluded' in exported
        with pytest.raises(ValueError, match='Automatic verification is unsupported'):
            verification.original_input(draft)
        corrected = deepcopy(draft['brief'])
        corrected['answers'][0]['text'] = 'Approval is required; suspended members are excluded.'
        reviewed = await inv.review_investigation(scope, draft['id'], 'correct', 'fixture reviewer',
                                                 'Fixture correction', corrected_brief=corrected)
        assert reviewed['brief'] == draft['brief']
        assert 'passage_presentation' not in reviewed['review']['corrected_brief']['answers'][0]
        await inv.record_outcome(scope, draft['id'], 'fixture reviewer', 'inconclusive', 'Fixture outcome')
        replacement = await plans.propose_plan('w', scope.collection_ids, claim, plan)
        await plans.supersede_plan('w', scope.collection_ids, first, replacement, 'fixture reviewer', 'Fixture supersession')
        with pytest.raises(LookupError):
            await inv.get_investigation(scope, draft['id'])
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await routes.export(draft['id'], scope.workspace, list(scope.collection_ids))
        assert await inv.list_investigations(scope) == []
    asyncio.run(run())


@pytest.mark.parametrize('was_approved', [False, True])
def test_plan_change_during_generation_is_never_saved(tmp_path, monkeypatch, was_approved):
    async def run():
        path, scope, claim, plan, first = await setup(tmp_path, monkeypatch, approve_plan=was_approved)
        async def change():
            if was_approved:
                replacement = await plans.propose_plan('w', scope.collection_ids, claim, plan)
                await plans.supersede_plan('w', scope.collection_ids, first, replacement, 'fixture reviewer', 'Fixture replacement')
            else:
                await plans.approve_plan('w', scope.collection_ids, first, 'fixture reviewer', 'Fixture approval during inference')
        runtime = Runtime(change=change, fallback=not was_approved)
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        with pytest.raises(LookupError, match='source plans'):
            await create(scope)
        assert len(runtime.calls) == 2
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT COUNT(*) FROM investigations').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM investigation_bindings').fetchone()[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize('tamper', ['missing_receipts', 'receipt_metadata', 'receipt_plan', 'raw',
                                   'mode', 'strip_all_markers', 'answer', 'storage_mode'])
def test_persisted_tampering_fails_reads_and_exports(tmp_path, monkeypatch, tamper):
    async def run():
        path, scope, claim, _, _ = await setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        draft = await create(scope)
        provenance = deepcopy(draft['provenance'])
        brief = deepcopy(draft['brief'])
        if tamper == 'missing_receipts':
            del provenance[bindings.RECEIPTS_KEY]
        elif tamper == 'receipt_metadata':
            provenance[bindings.RECEIPTS_KEY]['claim:' + claim]['reviewer'] = 'forged reviewer'
        elif tamper == 'receipt_plan':
            provenance[bindings.RECEIPTS_KEY]['claim:' + claim]['plan']['units'][1]['required_unit_ids'] = []
        elif tamper == 'raw':
            provenance['reviewed_claim_selection']['raw'] = '{}'
        elif tamper == 'mode':
            provenance['presentation_mode'] = 'reviewed-claim-selection-v14'
        elif tamper == 'strip_all_markers':
            provenance.pop('presentation_mode')
            provenance.pop(bindings.RECEIPTS_KEY)
            provenance.pop('reviewed_claim_selection')
        elif tamper == 'answer':
            brief['answers'][0]['text'] = 'forged answer'
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET provenance=?,brief=? WHERE id=?',
                       (json.dumps(provenance), json.dumps(brief), draft['id']))
            if tamper == 'storage_mode':
                db.execute('UPDATE investigations SET unit_plan_contract=NULL WHERE id=?', (draft['id'],))
        with pytest.raises((ValueError, LookupError)):
            await inv.get_investigation(scope, draft['id'])
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await routes.export(draft['id'], scope.workspace, list(scope.collection_ids))
    asyncio.run(run())


@pytest.mark.parametrize('stage,expected_calls', [('first', 1), ('second', 2)])
def test_invalid_stage_never_retries_or_saves(tmp_path, monkeypatch, stage, expected_calls):
    async def run():
        path, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        runtime = Runtime(failure=stage)
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        with pytest.raises((ValueError, inv.InvestigationInferenceError)):
            await create(scope)
        assert len(runtime.calls) == expected_calls
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT COUNT(*) FROM investigations').fetchone()[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['accept', 'correct', 'reject', 'outcome'])
def test_plan_change_after_initial_read_cannot_commit_review_or_outcome(tmp_path, monkeypatch, operation):
    async def run():
        path, scope, claim, plan, first = await setup(tmp_path, monkeypatch)
        runtime = Runtime()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        draft = await create(scope)
        if operation == 'outcome':
            await inv.review_investigation(scope, draft['id'], 'accept', 'fixture reviewer', 'Fixture acceptance')
        real_get = inv.get_investigation
        changed = False
        async def read_then_supersede(*args, **kwargs):
            nonlocal changed
            record = await real_get(*args, **kwargs)
            if not changed:
                changed = True
                replacement = await plans.propose_plan('w', scope.collection_ids, claim, plan)
                await plans.supersede_plan('w', scope.collection_ids, first, replacement,
                                           'fixture reviewer', 'Fixture race after read')
            return record
        monkeypatch.setattr(inv, 'get_investigation', read_then_supersede)
        with pytest.raises(LookupError, match='source plans'):
            if operation == 'outcome':
                await inv.record_outcome(scope, draft['id'], 'fixture reviewer', 'resolved', 'Fixture race')
            else:
                correction = deepcopy(draft['brief']) if operation == 'correct' else None
                await inv.review_investigation(scope, draft['id'], operation, 'fixture reviewer',
                                               'Fixture race', corrected_brief=correction)
        with sqlite3.connect(path) as db:
            status, review, outcome = db.execute(
                'SELECT status,review,outcome FROM investigations WHERE id=?', (draft['id'],)).fetchone()
            assert outcome is None
            assert status == ('reviewed' if operation == 'outcome' else 'draft')
            assert (review is not None) == (operation == 'outcome')
        assert len(runtime.calls) == 2
    asyncio.run(run())


def test_current_fallback_works_then_new_plan_invalidates_its_saved_record(tmp_path, monkeypatch):
    async def run():
        path, scope, claim, _, first = await setup(tmp_path, monkeypatch, approve_plan=False)
        monkeypatch.setattr(inv, 'get_runtime', lambda: Runtime(fallback=True))
        draft = await create(scope)
        assert draft['provenance'][bindings.RECEIPTS_KEY] == {'claim:' + claim: None}
        assert draft['brief']['answers'][0]['text'] == BODY
        await plans.approve_plan('w', scope.collection_ids, first, 'fixture reviewer', 'Fixture later approval')
        with pytest.raises(LookupError):
            await inv.get_investigation(scope, draft['id'])
    asyncio.run(run())


def test_storage_marker_rolls_back_with_failed_save(tmp_path, monkeypatch):
    async def run():
        path, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        real_persist = bindings.persist_contract_in_transaction
        async def fail_after_marker(*args):
            await real_persist(*args)
            raise ValueError('Fixture rollback after marker')
        monkeypatch.setattr(bindings, 'persist_contract_in_transaction', fail_after_marker)
        with pytest.raises(ValueError, match='rollback after marker'):
            await create(scope)
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT COUNT(*) FROM investigations').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM investigation_bindings').fetchone()[0] == 0
            assert 'unit_plan_contract' not in {row[1] for row in db.execute('PRAGMA table_info(investigations)')}
    asyncio.run(run())


def test_existing_mode_get_review_list_survive_first_v15_storage_migration(tmp_path, monkeypatch):
    class LegacyRuntime(Runtime):
        async def chat(self, messages, **kwargs):
            self.calls.append(deepcopy((messages, kwargs)))
            schema = kwargs['response_format']['schema']
            field = 'unit_ids' if len(self.calls) == 1 else 'review_unit_ids'
            selected = schema['properties'][field]['items']['enum'][-1]
            data = {field: [selected]}
            if len(self.calls) == 2:
                data.update(missing_start_token_id='', missing_end_token_id='', conflicting=False)
            return {'choices': [{'message': {'content': json.dumps(data)}}]}
    async def run():
        path, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        old_runtime = LegacyRuntime()
        monkeypatch.setattr(inv, 'get_runtime', lambda: old_runtime)
        historical = await create(scope, mode='reviewed-claim-selection-v12')
        with sqlite3.connect(path) as db:
            assert 'unit_plan_contract' not in {row[1] for row in db.execute('PRAGMA table_info(investigations)')}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        current = await create(scope)
        reread = await inv.get_investigation(scope, historical['id'])
        assert reread == historical
        reviewed = await inv.review_investigation(scope, historical['id'], 'accept', 'fixture reviewer', 'Fixture legacy review')
        assert reviewed['brief'] == historical['brief']
        assert {entry['id'] for entry in await inv.list_investigations(scope)} == {historical['id'], current['id']}
        assert len(old_runtime.calls) == 2
    asyncio.run(run())
