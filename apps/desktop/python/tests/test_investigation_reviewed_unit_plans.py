"""Nullable unit-plan snapshots bind current approved claims and exact sources."""
import asyncio
from copy import deepcopy
import sqlite3

import aiosqlite
import pytest

from potluck_local.services import investigation_reviewed_unit_plans as binding
from potluck_local.services import reviewed_claim_unit_plans as plans
from potluck_local.services import investigation_reviewed_evidence as selector
from potluck_local.services.evidence_lookup import EvidenceScope
from test_reviewed_claim_unit_plan_integrity import setup_claim


async def setup(tmp_path, monkeypatch):
    path, file, collections, claim, plan, ref = await setup_claim(tmp_path, monkeypatch)
    scope = EvidenceScope('w', tuple(collections))
    async with aiosqlite.connect(path) as db:
        await db.execute('BEGIN')
        result = await selector.prepare_in_transaction(db, scope, 'Rule')
    evidence = result['result']['reviewed_claims']
    return path, file, scope, claim, plan, evidence


def item(evidence, receipts, mode=binding.VERSION):
    return {'evidence': evidence, 'provenance': {
        'presentation_mode': mode, binding.RECEIPTS_KEY: receipts}}


def test_absent_plan_is_frozen_but_proposal_alone_is_not_an_approval(tmp_path, monkeypatch):
    async def run():
        path, _, scope, claim, plan, evidence = await setup(tmp_path, monkeypatch)
        receipts = await binding.snapshot(scope, evidence)
        assert receipts == {'claim:' + claim: None}
        proposal = await plans.propose_plan('w', scope.collection_ids, claim, plan)
        assert await binding.snapshot(scope, evidence) == receipts
        await plans.approve_plan('w', scope.collection_ids, proposal, 'fixture reviewer', 'Fixture unit review')
        async with aiosqlite.connect(path) as db:
            await db.execute('BEGIN IMMEDIATE')
            with pytest.raises(LookupError, match='source plans'):
                await binding.validate_in_transaction(db, scope, item(evidence, receipts))
        assert (await binding.snapshot(scope, evidence))['claim:' + claim]['id'] == proposal
    asyncio.run(run())


def test_superseded_receipt_is_not_a_current_snapshot(tmp_path, monkeypatch):
    async def run():
        path, _, scope, claim, plan, evidence = await setup(tmp_path, monkeypatch)
        first = await plans.propose_plan('w', scope.collection_ids, claim, plan)
        await plans.approve_plan('w', scope.collection_ids, first, 'fixture reviewer', 'Fixture unit review')
        receipts = await binding.snapshot(scope, evidence)
        second = await plans.propose_plan('w', scope.collection_ids, claim, plan)
        await plans.supersede_plan('w', scope.collection_ids, first, second, 'fixture reviewer', 'Fixture replacement')
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute('BEGIN IMMEDIATE')
            with pytest.raises(LookupError):
                await binding.validate_in_transaction(db, scope, item(evidence, receipts))
        assert (await binding.snapshot(scope, evidence))['claim:' + claim]['id'] == second
    asyncio.run(run())


@pytest.mark.parametrize('field', ['content', 'reviewer', 'reviewed_at', 'status', 'id',
                                  'citation_id', 'source_content', 'source_citation', 'source_ref'])
def test_stored_evidence_must_match_current_claim_and_sources(tmp_path, monkeypatch, field):
    async def run():
        _, _, scope, _, _, evidence = await setup(tmp_path, monkeypatch)
        forged = deepcopy(evidence)
        if field == 'source_content':
            forged[0]['sources'][0]['content'] += ' forged'
        elif field == 'source_citation':
            forged[0]['sources'][0]['citation_id'] += ':forged'
        elif field == 'source_ref':
            forged[0]['sources'][0]['revision_id'] += 'forged'
        else:
            forged[0][field] += 'forged'
        with pytest.raises((ValueError, LookupError)):
            await binding.snapshot(scope, forged)
    asyncio.run(run())


def test_existing_transaction_never_committed_and_scope_is_checked(tmp_path, monkeypatch):
    async def run():
        path, _, scope, _, _, evidence = await setup(tmp_path, monkeypatch)
        receipts = await binding.snapshot(scope, evidence)
        async with aiosqlite.connect(path) as db:
            with pytest.raises(ValueError, match='transaction'):
                await binding.snapshot_in_transaction(db, scope, evidence)
            await db.execute('BEGIN IMMEDIATE')
            await db.execute('CREATE TABLE caller_write (value TEXT)')
            await db.execute("INSERT INTO caller_write VALUES ('pending')")
            assert await binding.snapshot_in_transaction(db, scope, evidence) == receipts
            await binding.validate_in_transaction(db, scope, item(evidence, receipts))
            assert db.in_transaction
            await db.rollback()
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT name FROM sqlite_master WHERE name='caller_write'").fetchone() is None
        with pytest.raises(LookupError):
            await binding.snapshot(EvidenceScope('other', scope.collection_ids), evidence)
        with pytest.raises(LookupError):
            await binding.snapshot(EvidenceScope('w', ('wrong',)), evidence)
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['absent', 'missing_claim', 'extra_claim', 'downgrade'])
def test_missing_receipts_and_mode_downgrade_fail_closed(tmp_path, monkeypatch, mutation):
    async def run():
        path, _, scope, _, _, evidence = await setup(tmp_path, monkeypatch)
        draft = item(evidence, await binding.snapshot(scope, evidence))
        if mutation == 'absent':
            del draft['provenance'][binding.RECEIPTS_KEY]
        elif mutation == 'missing_claim':
            draft['provenance'][binding.RECEIPTS_KEY] = {}
        elif mutation == 'extra_claim':
            draft['provenance'][binding.RECEIPTS_KEY]['claim:forged'] = None
        else:
            draft['provenance']['presentation_mode'] = 'reviewed-claim-selection-v14'
        async with aiosqlite.connect(path) as db:
            await db.execute('BEGIN IMMEDIATE')
            with pytest.raises((ValueError, LookupError)):
                await binding.validate_in_transaction(db, scope, draft)
    asyncio.run(run())


def test_failed_source_is_unavailable_even_when_no_plan_exists(tmp_path, monkeypatch):
    async def run():
        path, _, scope, _, _, evidence = await setup(tmp_path, monkeypatch)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE source_collections SET scan_status='failed'")
        with pytest.raises(LookupError):
            await binding.snapshot(scope, evidence)
    asyncio.run(run())


def test_historical_modes_do_not_create_plan_schema(tmp_path, monkeypatch):
    async def run():
        path, _, scope, _, _, evidence = await setup(tmp_path, monkeypatch)
        async with aiosqlite.connect(path) as db:
            await db.execute('BEGIN IMMEDIATE')
            await binding.validate_in_transaction(db, scope, {
                'evidence': evidence, 'provenance': {'presentation_mode': 'reviewed-claim-selection-v12'}})
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE name='reviewed_claim_unit_plans'")
            assert await cursor.fetchone() is None
    asyncio.run(run())
