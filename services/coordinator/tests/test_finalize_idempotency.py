"""Idempotency of `_finalize_job_in_db` (routers/jobs.py) itself, not just
its inner `record_job_credits` call.

`record_job_credits` was already made idempotent via a UNIQUE (job_id,
direction) constraint + ON CONFLICT DO NOTHING (migration 0003; see
test_credit_ledger_idempotency.py). This file proves the guarantee holds
end-to-end through `_finalize_job_in_db` — the actual function the
streaming finalizer's `finally:` block invokes, including its own DB-row
update (jobs.status/output_tokens/duration_ms/completed_at) — for the
scenario the pool-reliability spine cares about: a duplicate finalize call
for the same job_id (e.g. a finalize firing after a drain, or a retried
background task) must never write the credit rows twice.

`_finalize_job_in_db` opens its own session via
`coordinator.db.database.get_session_factory()` (by design — it must
survive the request-scoped session closing), so these tests bring up the
real module-global engine via init_db()/close_db() rather than the
`db_session` single-transaction fixture used elsewhere. Fixture rows are
committed directly and cleaned up in `finally`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.db.database import close_db, get_session_factory, init_db
from coordinator.models.credit import CreditLedger
from coordinator.models.job import Job
from coordinator.models.peer import Peer
from coordinator.routers.jobs import _finalize_job_in_db

_MODEL = "qwen2.5:3b-instruct-q4_K_M"


async def _count_ledger_rows(db: AsyncSession, job_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).where(CreditLedger.job_id == job_id)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_double_finalize_writes_credits_once() -> None:
    """Two `_finalize_job_in_db` calls for the same job_id (simulating a
    duplicate/retried finalize task) must produce exactly one spent + one
    earned row, not two of each."""
    await init_db()
    factory = get_session_factory()

    requester_id = uuid.uuid4()
    contributor_owner_id = uuid.uuid4()
    peer_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None

    try:
        # ---- Fixture: a peer (distinct owner from requester -> public-pool
        # job, so record_job_credits actually writes both rows) + a
        # dispatched job row for it to finalize. ----
        async with factory() as db:
            peer = Peer(
                member_id=contributor_owner_id,
                public_key=uuid.uuid4().hex,
                status="active",
            )
            db.add(peer)
            await db.flush()
            peer_id = peer.id

            job = Job(
                member_id=requester_id,
                peer_id=peer.id,
                model_slug=_MODEL,
                privacy_mode="pseudonymous",
                status="dispatched",
            )
            db.add(job)
            await db.flush()
            job_id = job.id
            await db.commit()

        # ---- Call the real finalize path twice with identical results,
        # exactly like a finalize firing twice for the same job_id would. ----
        for _ in range(2):
            await _finalize_job_in_db(
                job_id=job_id,
                error_category=None,
                error_message=None,
                output_tokens=100,
                duration_ms=1234,
            )

        async with factory() as db:
            count = await _count_ledger_rows(db, job_id)
            assert count == 2, (
                f"double _finalize_job_in_db call created duplicate credit "
                f"rows — expected 2 (one spent + one earned), got {count}"
            )

            rows = (
                await db.execute(
                    select(CreditLedger).where(CreditLedger.job_id == job_id)
                )
            ).scalars().all()
            directions = {r.direction for r in rows}
            assert directions == {"spent", "earned"}

            # The jobs row itself also settled into exactly one terminal
            # state — no half-applied update from the second call.
            db_job = await db.get(Job, job_id)
            assert db_job is not None
            assert db_job.status == "completed"
            assert db_job.output_tokens == 100
            assert db_job.duration_ms == 1234
            assert db_job.completed_at is not None
    finally:
        async with factory() as cleanup_db:
            async with cleanup_db.begin():
                if job_id is not None:
                    await cleanup_db.execute(
                        delete(CreditLedger).where(CreditLedger.job_id == job_id)
                    )
                    await cleanup_db.execute(delete(Job).where(Job.id == job_id))
                if peer_id is not None:
                    await cleanup_db.execute(delete(Peer).where(Peer.id == peer_id))
        await close_db()


@pytest.mark.asyncio
async def test_double_finalize_self_served_writes_credits_once() -> None:
    """Same guarantee for the self-served path (requester == contributor
    owner) — finalize skips the ledger entirely for self-served jobs, so
    this proves the double-call doesn't somehow write anything either."""
    await init_db()
    factory = get_session_factory()

    member_id = uuid.uuid4()
    peer_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None

    try:
        async with factory() as db:
            peer = Peer(
                member_id=member_id,
                public_key=uuid.uuid4().hex,
                status="active",
            )
            db.add(peer)
            await db.flush()
            peer_id = peer.id

            job = Job(
                member_id=member_id,
                peer_id=peer.id,
                model_slug=_MODEL,
                privacy_mode="pseudonymous",
                status="dispatched",
            )
            db.add(job)
            await db.flush()
            job_id = job.id
            await db.commit()

        for _ in range(2):
            await _finalize_job_in_db(
                job_id=job_id,
                error_category=None,
                error_message=None,
                output_tokens=50,
                duration_ms=100,
            )

        async with factory() as db:
            count = await _count_ledger_rows(db, job_id)
            assert count == 0, "self-served job must never write ledger rows"
    finally:
        async with factory() as cleanup_db:
            async with cleanup_db.begin():
                if job_id is not None:
                    await cleanup_db.execute(
                        delete(CreditLedger).where(CreditLedger.job_id == job_id)
                    )
                    await cleanup_db.execute(delete(Job).where(Job.id == job_id))
                if peer_id is not None:
                    await cleanup_db.execute(delete(Peer).where(Peer.id == peer_id))
        await close_db()
