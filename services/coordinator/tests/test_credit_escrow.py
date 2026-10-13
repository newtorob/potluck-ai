"""Escrow + verification tests for the credit ledger.

Cross-member (public-pool) earned credits are escrowed until a verification
result releases them; self-served / household credits are never escrowed.
apply_verification_result releases (pass) or forfeits-by-leaving-escrowed
(fail) and nudges the contributor peer's reputation.

Requires a live Postgres via the db_session fixture (writes rolled back).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.credit import CreditLedger
from coordinator.models.job import Job
from coordinator.models.peer import Peer, PeerHeartbeat
from coordinator.services.credit_ledger import (
    REPUTATION_STEP,
    apply_verification_result,
    record_job_credits,
)

_MODEL = "qwen2.5:3b-instruct-q4_K_M"


async def _insert_job(db: AsyncSession) -> uuid.UUID:
    job = Job(member_id=uuid.uuid4(), model_slug=_MODEL, status="completed")
    db.add(job)
    await db.flush()
    return job.id


async def _earned_row(db: AsyncSession, job_id: uuid.UUID) -> CreditLedger:
    return (
        await db.execute(
            select(CreditLedger).where(
                CreditLedger.job_id == job_id, CreditLedger.direction == "earned"
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_cross_member_earned_is_escrowed(db_session: AsyncSession) -> None:
    job_id = await _insert_job(db_session)
    spent, earned = await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=uuid.uuid4(),
        contributor_owner_member_id=uuid.uuid4(),  # different -> public pool
        model_slug=_MODEL,
        output_tokens=100,
    )
    assert earned.escrowed is True, "cross-member earned credits must be escrowed"
    assert spent.escrowed is False, "the spent (debit) row is never escrowed"


@pytest.mark.asyncio
async def test_self_served_earned_is_not_escrowed(db_session: AsyncSession) -> None:
    job_id = await _insert_job(db_session)
    member = uuid.uuid4()
    _, earned = await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=member,
        contributor_owner_member_id=member,  # self-served / household
        model_slug=_MODEL,
        output_tokens=100,
    )
    assert earned.escrowed is False, "self-served credits must not be escrowed"


@pytest.mark.asyncio
async def test_verification_pass_releases_escrow(db_session: AsyncSession) -> None:
    job_id = await _insert_job(db_session)
    await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=uuid.uuid4(),
        contributor_owner_member_id=uuid.uuid4(),
        model_slug=_MODEL,
        output_tokens=100,
    )
    released = await apply_verification_result(db_session, job_id=job_id, passed=True)
    assert released is True
    assert (await _earned_row(db_session, job_id)).escrowed is False

    # Idempotent: releasing an already-released row is a no-op.
    again = await apply_verification_result(db_session, job_id=job_id, passed=True)
    assert again is False


@pytest.mark.asyncio
async def test_verification_fail_keeps_escrow(db_session: AsyncSession) -> None:
    job_id = await _insert_job(db_session)
    await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=uuid.uuid4(),
        contributor_owner_member_id=uuid.uuid4(),
        model_slug=_MODEL,
        output_tokens=100,
    )
    released = await apply_verification_result(db_session, job_id=job_id, passed=False)
    assert released is False
    assert (await _earned_row(db_session, job_id)).escrowed is True, (
        "a failed verification forfeits credits - they stay escrowed forever"
    )


@pytest.mark.asyncio
async def test_verification_nudges_reputation(db_session: AsyncSession) -> None:
    peer = Peer(member_id=uuid.uuid4(), public_key=f"pk-{uuid.uuid4()}")
    db_session.add(peer)
    await db_session.flush()
    hb = PeerHeartbeat(
        peer_id=peer.id,
        last_beat_at=datetime.now(timezone.utc),
        binary_hash="h",
        client_version="v",
    )
    db_session.add(hb)
    await db_session.flush()
    assert hb.reputation == pytest.approx(0.5)

    job_id = await _insert_job(db_session)
    await record_job_credits(
        db_session,
        job_id=job_id,
        requester_member_id=uuid.uuid4(),
        contributor_owner_member_id=uuid.uuid4(),
        model_slug=_MODEL,
        output_tokens=100,
    )
    await apply_verification_result(
        db_session, job_id=job_id, passed=True, contributor_peer_id=peer.id
    )
    await db_session.refresh(hb)
    assert hb.reputation == pytest.approx(0.5 + REPUTATION_STEP), (
        "a passed verification should raise the contributor peer's reputation"
    )
