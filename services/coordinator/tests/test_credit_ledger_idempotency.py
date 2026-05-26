"""Idempotency tests for record_job_credits.

Verifies that calling record_job_credits twice for the same job_id:
  - produces exactly two ledger rows (not four)
  - returns identical (spent, earned) data on both calls
  - leaves the hash chain intact and in the same state

Also runs a self-served-job path (requester == contributor_owner) to
confirm the in-transaction chain construction still works correctly
after the ON CONFLICT change.

These tests require a live Postgres — they use the `db_session` fixture
from conftest.py which wraps each test in a SAVEPOINT that is rolled
back afterward.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.credit import CreditLedger
from coordinator.models.job import Job
from coordinator.services.credit_ledger import (
    GENESIS_HASH,
    _compute_row_hash,
    record_job_credits,
)


async def _insert_job(db: AsyncSession) -> uuid.UUID:
    """Insert a minimal completed job row and return its id."""
    job = Job(
        member_id=uuid.uuid4(),
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        status="completed",
    )
    db.add(job)
    await db.flush()
    assert job.id is not None
    return job.id


async def _count_ledger_rows_for_job(db: AsyncSession, job_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).where(CreditLedger.job_id == job_id)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_first_call_writes_two_rows(db_session: AsyncSession) -> None:
    """Baseline: a fresh job gets exactly one spent + one earned row."""
    job_id = await _insert_job(db_session)
    requester = uuid.uuid4()
    contributor = uuid.uuid4()

    spent, earned = await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=requester,
        contributor_owner_member_id=contributor,
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        output_tokens=100,
    )

    count = await _count_ledger_rows_for_job(db_session, job_id)
    assert count == 2, f"expected 2 rows, got {count}"
    assert spent.direction == "spent"
    assert earned.direction == "earned"
    assert spent.job_id == job_id
    assert earned.job_id == job_id


@pytest.mark.asyncio
async def test_second_call_is_no_op_returns_same_rows(db_session: AsyncSession) -> None:
    """Core idempotency: second call must not create new rows and must
    return data identical to the first call."""
    job_id = await _insert_job(db_session)
    requester = uuid.uuid4()
    contributor = uuid.uuid4()

    spent1, earned1 = await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=requester,
        contributor_owner_member_id=contributor,
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        output_tokens=100,
    )

    # Simulate the duplicate task firing.
    spent2, earned2 = await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=requester,
        contributor_owner_member_id=contributor,
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        output_tokens=100,
    )

    # Still only two rows in the DB.
    count = await _count_ledger_rows_for_job(db_session, job_id)
    assert count == 2, (
        f"duplicate record_job_credits created new rows — expected 2, got {count}"
    )

    # Returned data is identical across both calls.
    assert spent1.id == spent2.id, "spent row id changed on second call"
    assert earned1.id == earned2.id, "earned row id changed on second call"
    assert spent1.row_hash == spent2.row_hash, "spent row_hash changed on second call"
    assert earned1.row_hash == earned2.row_hash, "earned row_hash changed on second call"
    assert spent1.credits == spent2.credits
    assert earned1.credits == earned2.credits


@pytest.mark.asyncio
async def test_hash_chain_is_correct_after_first_call(db_session: AsyncSession) -> None:
    """The hash on the inserted rows matches what _compute_row_hash produces
    independently. This confirms the chain is intact and the no-op second
    call cannot corrupt it."""
    job_id = await _insert_job(db_session)
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    model_slug = "qwen2.5:3b-instruct-q4_K_M"
    output_tokens = 50
    multiplier = Decimal("1.00")

    spent, earned = await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=requester,
        contributor_owner_member_id=contributor,
        model_slug=model_slug,
        output_tokens=output_tokens,
    )

    # Both members have no prior rows, so prev_row_hash == GENESIS_HASH.
    expected_spent_hash = _compute_row_hash(
        member_id=requester,
        direction="spent",
        job_id=job_id,
        model_slug=model_slug,
        output_tokens=output_tokens,
        multiplier=multiplier,
        credits=Decimal(output_tokens) * multiplier,
        prev_row_hash=GENESIS_HASH,
    )
    assert spent.prev_row_hash == GENESIS_HASH
    assert spent.row_hash == expected_spent_hash

    # Contributor is a distinct member so their chain also starts at GENESIS.
    from coordinator.services.credit_ledger import CONTRIBUTION_BONUS_MULTIPLIER

    earn_multiplier = multiplier * CONTRIBUTION_BONUS_MULTIPLIER
    expected_earned_hash = _compute_row_hash(
        member_id=contributor,
        direction="earned",
        job_id=job_id,
        model_slug=model_slug,
        output_tokens=output_tokens,
        multiplier=earn_multiplier,
        credits=Decimal(output_tokens) * multiplier * CONTRIBUTION_BONUS_MULTIPLIER,
        prev_row_hash=GENESIS_HASH,
    )
    assert earned.prev_row_hash == GENESIS_HASH
    assert earned.row_hash == expected_earned_hash


@pytest.mark.asyncio
async def test_hash_chain_unchanged_after_no_op_second_call(
    db_session: AsyncSession,
) -> None:
    """After a no-op second call, hash chain state in the DB is identical
    to what the first call produced. No spurious chain links are added."""
    job_id = await _insert_job(db_session)
    requester = uuid.uuid4()
    contributor = uuid.uuid4()

    spent1, earned1 = await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=requester,
        contributor_owner_member_id=contributor,
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        output_tokens=75,
    )

    spent2, earned2 = await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=requester,
        contributor_owner_member_id=contributor,
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        output_tokens=75,
    )

    # Chain fields are byte-identical.
    assert spent1.prev_row_hash == spent2.prev_row_hash
    assert spent1.row_hash == spent2.row_hash
    assert earned1.prev_row_hash == earned2.prev_row_hash
    assert earned1.row_hash == earned2.row_hash

    # Total row count in the DB is still 2.
    count = await _count_ledger_rows_for_job(db_session, job_id)
    assert count == 2


@pytest.mark.asyncio
async def test_self_served_job_chain_construction(db_session: AsyncSession) -> None:
    """When requester == contributor_owner (self-served), earned.prev_row_hash
    must equal spent.row_hash — verifying the in-transaction chain ordering
    still works after the ON CONFLICT change."""
    job_id = await _insert_job(db_session)
    member = uuid.uuid4()

    spent, earned = await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=member,
        contributor_owner_member_id=member,
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        output_tokens=200,
    )

    assert spent.member_id == member
    assert earned.member_id == member
    assert earned.prev_row_hash == spent.row_hash, (
        "self-served job: earned row must chain onto spent row. "
        f"earned.prev_row_hash={earned.prev_row_hash!r}, "
        f"spent.row_hash={spent.row_hash!r}"
    )


@pytest.mark.asyncio
async def test_self_served_job_idempotency(db_session: AsyncSession) -> None:
    """Self-served jobs must also be idempotent — second call returns the
    same rows and does not create additional links in the member's chain."""
    job_id = await _insert_job(db_session)
    member = uuid.uuid4()

    spent1, earned1 = await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=member,
        contributor_owner_member_id=member,
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        output_tokens=200,
    )

    spent2, earned2 = await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=member,
        contributor_owner_member_id=member,
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        output_tokens=200,
    )

    count = await _count_ledger_rows_for_job(db_session, job_id)
    assert count == 2
    assert spent1.id == spent2.id
    assert earned1.id == earned2.id
    assert spent1.row_hash == spent2.row_hash
    assert earned1.row_hash == earned2.row_hash
