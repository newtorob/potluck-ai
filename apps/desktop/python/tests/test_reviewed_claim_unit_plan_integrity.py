"""Authority, transaction and tamper regressions for explicit unit plans."""
import asyncio
import json
import sqlite3
from copy import deepcopy

import aiosqlite
import pytest

from potluck_local.services import reviewed_claim_unit_plans as plans
from potluck_local.services import reviewed_knowledge as knowledge
from potluck_local.services import source_collections as sources
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.reviewed_source_unit_plan import text_hash


async def setup_claim(tmp_path, monkeypatch, *, content='Rule applies.', approved=True):
    path = tmp_path / 'memory.db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(path))
    folder = tmp_path / 'docs'
    folder.mkdir()
    file = folder / 'policy.md'
    file.write_text('# Source\n\nFixture evidence')
    await init_memory_db()
    collection = await sources.register_collection('w', 'docs', folder)
    await sources.scan_collection('w', collection)
    with sqlite3.connect(path) as db:
        document, revision = db.execute('SELECT id,revision_id FROM source_documents').fetchone()
        chunk = db.execute('SELECT id FROM source_chunks').fetchone()[0]
    ref = dict(collection_id=collection, document_id=document, revision_id=revision, chunk_id=chunk)
    scope = [collection]
    claim = await knowledge.propose_claim('w', scope, 'policy', content, [ref])
    if approved:
        await knowledge.approve_claim('w', scope, claim, 'source reviewer', 'Fixture approval')
    plan = {'source_sha256': text_hash(content), 'units': [
        {'id': 'rule', 'ranges': [[0, len(content)]], 'required_unit_ids': []}]}
    return path, file, scope, claim, plan, ref


async def approved_plan(tmp_path, monkeypatch):
    state = await setup_claim(tmp_path, monkeypatch)
    path, file, scope, claim, plan, ref = state
    plan_id = await plans.propose_plan('w', scope, claim, plan)
    await plans.approve_plan('w', scope, plan_id, 'plan reviewer', 'Reviewed complete units')
    return (*state, plan_id, await plans.get_approved_plan('w', scope, claim))


def test_plan_requires_separate_explicit_review_of_an_approved_claim(tmp_path, monkeypatch):
    async def run():
        path, _, scope, claim, plan, _ = await setup_claim(tmp_path, monkeypatch, approved=False)
        with pytest.raises(LookupError):
            await plans.propose_plan('w', scope, claim, plan)
        await knowledge.approve_claim('w', scope, claim, 'source reviewer', 'Reviewed source')
        forged = {**plan, 'status': 'approved'}
        with pytest.raises(ValueError):
            await plans.propose_plan('w', scope, claim, forged)
        plan_id = await plans.propose_plan('w', scope, claim, plan)
        assert await plans.get_approved_plan('w', scope, claim) is None
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT status,reviewer FROM reviewed_claim_unit_plans').fetchall() == [('proposed', None)]
        await plans.approve_plan('w', scope, plan_id, 'plan reviewer', 'Reviewed units')
        assert (await plans.get_approved_plan('w', scope, claim))['id'] == plan_id
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [
    ('reviewer', 'different reviewer'),
    ('reviewed_at', '2026-01-01T00:00:00+00:00'),
    ('content', 'Changed source claim'),
])
def test_source_approval_binding_drift_rejects_receipt(tmp_path, monkeypatch, field, value):
    async def run():
        path, _, scope, claim, _, _, _, receipt = await approved_plan(tmp_path, monkeypatch)
        # Column identifiers are fixed by the parametrized test, never caller input.
        with sqlite3.connect(path) as db:
            db.execute(f'UPDATE reviewed_claims SET {field}=? WHERE id=?', (value, claim))
        with pytest.raises(ValueError):
            await plans.get_approved_plan('w', scope, claim)
        async with aiosqlite.connect(path) as db:
            await db.execute('BEGIN IMMEDIATE')
            with pytest.raises(ValueError):
                await plans.validate_receipt_in_transaction(db, 'w', scope, claim, receipt)
            await db.rollback()
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [
    ('plan_json', '{"units":[]}'),
    ('plan_sha256', '0' * 64),
    ('binding_json', '{}'),
    ('revision', 'not-a-uuid'),
    ('reviewer', None),
    ('reason', ''),
    ('reviewed_at', None),
    ('created_at', ''),
    ('status', 'proposed'),
    ('status', 'superseded'),
    ('superseded_by', '11111111-1111-1111-1111-111111111111'),
])
def test_plan_content_and_state_tampering_rejects_instead_of_fallback(tmp_path, monkeypatch, field, value):
    async def run():
        path, _, scope, claim, _, _, plan_id, _ = await approved_plan(tmp_path, monkeypatch)
        with sqlite3.connect(path) as db:
            db.execute(f'UPDATE reviewed_claim_unit_plans SET {field}=? WHERE id=?', (value, plan_id))
        with pytest.raises(ValueError):
            await plans.get_approved_plan('w', scope, claim)
    asyncio.run(run())


def test_proposed_metadata_cannot_masquerade_as_review(tmp_path, monkeypatch):
    async def run():
        path, _, scope, claim, plan, _ = await setup_claim(tmp_path, monkeypatch)
        plan_id = await plans.propose_plan('w', scope, claim, plan)
        with sqlite3.connect(path) as db:
            db.execute('UPDATE reviewed_claim_unit_plans SET reason=? WHERE id=?', ('forged review', plan_id))
        with pytest.raises(ValueError):
            await plans.get_approved_plan('w', scope, claim)
        with pytest.raises(ValueError):
            await plans.approve_plan('w', scope, plan_id, 'reviewer', 'Cannot approve corrupt proposal')
    asyncio.run(run())


def test_failed_scan_without_an_approved_plan_is_not_whole_claim_fallback(tmp_path, monkeypatch):
    async def run():
        _, file, scope, claim, _, _ = await setup_claim(tmp_path, monkeypatch)
        assert await plans.get_approved_plan('w', scope, claim) is None
        file.write_bytes(b'\xff')
        with pytest.raises(UnicodeError):
            await sources.scan_collection('w', scope[0])
        with pytest.raises(LookupError):
            await plans.get_approved_plan('w', scope, claim)
    asyncio.run(run())


def test_supersession_cannot_cross_claim_and_rolls_back(tmp_path, monkeypatch):
    async def run():
        path, _, scope, claim, plan, ref, first, receipt = await approved_plan(tmp_path, monkeypatch)
        other = await knowledge.propose_claim('w', scope, 'other', 'Rule applies.', [ref])
        await knowledge.approve_claim('w', scope, other, 'source reviewer', 'Second claim')
        replacement = await plans.propose_plan('w', scope, other, plan)
        with pytest.raises(ValueError):
            await plans.supersede_plan('w', scope, first, replacement, 'reviewer', 'Wrong claim')
        assert await plans.get_approved_plan('w', scope, claim) == receipt
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT status FROM reviewed_claim_unit_plans WHERE id=?', (replacement,)).fetchone() == ('proposed',)
    asyncio.run(run())


def test_unusable_dependency_closure_is_never_approved(tmp_path, monkeypatch):
    async def run():
        path, _, scope, claim, _, _ = await setup_claim(tmp_path, monkeypatch, content='x' * 17)
        plan = {'source_sha256': text_hash('x' * 17), 'units': [
            {'id': f'U{i}', 'ranges': [[i, i + 1]], 'required_unit_ids': [f'U{i-1}'] if i else []}
            for i in range(17)]}
        plan_id = await plans.propose_plan('w', scope, claim, plan)
        with pytest.raises(ValueError, match='closed'):
            await plans.approve_plan('w', scope, plan_id, 'reviewer', 'Too many dependencies')
        assert await plans.get_approved_plan('w', scope, claim) is None
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT status,reviewed_at FROM reviewed_claim_unit_plans WHERE id=?', (plan_id,)).fetchone() == ('proposed', None)
    asyncio.run(run())


def test_same_connection_lookup_preserves_callers_transaction_and_row_factory(tmp_path, monkeypatch):
    async def run():
        path, _, scope, claim, plan, _, first, old_receipt = await approved_plan(tmp_path, monkeypatch)
        replacement = await plans.propose_plan('w', scope, claim, plan)
        await plans.supersede_plan('w', scope, first, replacement, 'reviewer', 'Updated units')
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE caller_write(value TEXT)')
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute('BEGIN IMMEDIATE')
            await db.execute('INSERT INTO caller_write VALUES (?)', ('must roll back',))
            receipt = await plans.get_approved_plan_in_transaction(db, 'w', scope, claim)
            assert receipt['id'] == replacement
            assert await plans.validate_receipt_in_transaction(db, 'w', scope, claim, receipt) == receipt
            with pytest.raises(ValueError):
                await plans.validate_receipt_in_transaction(db, 'w', scope, claim, old_receipt)
            forged = deepcopy(receipt)
            forged['approved'] = True
            with pytest.raises(ValueError):
                await plans.validate_receipt_in_transaction(db, 'w', scope, claim, forged)
            assert db.in_transaction
            await db.rollback()
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT count(*) FROM caller_write').fetchone()[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['claim_delete', 'source_revision', 'collection_remove'])
def test_source_erasure_scrubs_plan_history_and_review_notes(tmp_path, monkeypatch, operation):
    async def run():
        path, file, scope, claim, plan, _, first, _ = await approved_plan(tmp_path, monkeypatch)
        second = await plans.propose_plan('w', scope, claim, plan)
        await plans.supersede_plan('w', scope, first, second, 'reviewer', 'Private review note')
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT count(*) FROM reviewed_claim_unit_plans').fetchone()[0] == 2
        if operation == 'claim_delete':
            await knowledge.delete_claim('w', scope, claim)
        elif operation == 'source_revision':
            file.write_text('# Replacement\n\nChanged source')
            await sources.scan_collection('w', scope[0])
        else:
            await sources.remove_collection('w', scope[0])
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT count(*) FROM reviewed_claim_unit_plans').fetchone()[0] == 0
        with pytest.raises(LookupError):
            await plans.get_approved_plan('w', scope, claim)
    asyncio.run(run())


def test_plan_credential_payload_rejected_and_review_metadata_redacted(tmp_path, monkeypatch):
    async def run():
        path, _, scope, claim, plan, _ = await setup_claim(tmp_path, monkeypatch)
        token = 'sk-proj-' + 'A' * 32
        bad = deepcopy(plan)
        bad['units'][0]['id'] = token
        with pytest.raises(ValueError, match='sensitive'):
            await plans.propose_plan('w', scope, claim, bad)
        plan_id = await plans.propose_plan('w', scope, claim, plan)
        await plans.approve_plan('w', scope, plan_id, 'reviewer', f'Review note {token}')
        with sqlite3.connect(path) as db:
            notes = db.execute('SELECT reason FROM reviewed_claim_unit_plans').fetchone()[0]
        assert token not in notes
        assert (await plans.get_approved_plan('w', scope, claim))['id'] == plan_id
    asyncio.run(run())
