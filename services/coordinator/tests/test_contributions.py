"""Tests for GET /me/contributions (per-machine contribution receipts).

Calls the endpoint function directly against the db_session fixture (the
dominant pattern in this suite). NOTE: db_session is backed by a live DB that
may already contain the developer's own peers/jobs, and only THIS test's writes
are rolled back. So assertions are scoped to uniquely-named seed peers rather
than global totals. Verifies: own-machine scoping (other members excluded), the
completed + in-window job filter, per-machine served/tokens, and per-machine
credits attributed via the ledger -> job -> peer join.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.constants import STUB_MEMBER_ID
from coordinator.models.credit import CreditLedger
from coordinator.models.job import Job
from coordinator.models.peer import Peer, PeerHeartbeat
from coordinator.routers.me import my_contributions

NOW = datetime.now(timezone.utc)


async def _peer(
    db: AsyncSession, member_id: uuid.UUID, hostname: str, *, reputation: float | None = None
) -> uuid.UUID:
    p = Peer(
        member_id=member_id,
        public_key=uuid.uuid4().hex,
        hostname=hostname,
        hardware_summary={},
        status="active",
    )
    db.add(p)
    await db.flush()
    if reputation is not None:
        db.add(
            PeerHeartbeat(
                peer_id=p.id,
                last_beat_at=NOW,
                reputation=reputation,
                binary_hash="testhash",
                client_version="0.0.0",
            )
        )
        await db.flush()
    return p.id


async def _job(
    db: AsyncSession,
    *,
    peer_id: uuid.UUID | None,
    requester: uuid.UUID,
    status: str = "completed",
    tokens: int = 0,
    completed_at: datetime | None = NOW,
) -> uuid.UUID:
    j = Job(
        member_id=requester,
        peer_id=peer_id,
        model_slug="qwen2.5:3b",
        status=status,
        output_tokens=tokens,
        completed_at=completed_at,
    )
    db.add(j)
    await db.flush()
    return j.id


@pytest.mark.asyncio
async def test_contributions_per_machine(db_session: AsyncSession) -> None:
    # Unique hostnames so lookups can't collide with pre-existing live data.
    tag = uuid.uuid4().hex[:8]
    h_alpha, h_beta, h_other = f"alpha-{tag}", f"beta-{tag}", f"notmine-{tag}"

    other_member = uuid.uuid4()
    alpha = await _peer(db_session, STUB_MEMBER_ID, h_alpha, reputation=0.5)
    beta = await _peer(db_session, STUB_MEMBER_ID, h_beta)  # no heartbeat -> reputation None
    not_mine = await _peer(db_session, other_member, h_other)

    # alpha: 2 completed in-window (150 tokens), 1 dispatched (excluded),
    # 1 completed but stale (excluded).
    j_a1 = await _job(db_session, peer_id=alpha, requester=STUB_MEMBER_ID, tokens=100)
    await _job(db_session, peer_id=alpha, requester=STUB_MEMBER_ID, tokens=50)
    await _job(db_session, peer_id=alpha, requester=STUB_MEMBER_ID, status="dispatched", tokens=999)
    await _job(
        db_session, peer_id=alpha, requester=STUB_MEMBER_ID, tokens=999,
        completed_at=NOW - timedelta(days=30),
    )
    # beta: 1 completed in-window (200 tokens).
    await _job(db_session, peer_id=beta, requester=STUB_MEMBER_ID, tokens=200)
    # not-mine did work for another member: must NOT appear in my receipt.
    await _job(db_session, peer_id=not_mine, requester=other_member, tokens=500)

    # A cross-member earned credit attributed to alpha via its job j_a1.
    db_session.add(
        CreditLedger(
            member_id=STUB_MEMBER_ID,
            direction="earned",
            job_id=j_a1,
            model_slug="qwen2.5:3b",
            output_tokens=100,
            multiplier=Decimal("1.00"),
            credits=Decimal("5.00"),
            prev_row_hash="0" * 64,
            row_hash="1" * 64,
        )
    )
    await db_session.flush()

    resp = await my_contributions(days=7, db=db_session, member_id=STUB_MEMBER_ID)
    by_host = {m.hostname: m for m in resp.machines}

    # My two machines appear with the expected per-machine values.
    assert h_alpha in by_host and h_beta in by_host
    assert by_host[h_alpha].requests_served == 2  # dispatched + stale excluded
    assert by_host[h_alpha].output_tokens == 150
    assert by_host[h_alpha].credits_earned == Decimal("5.00")
    assert by_host[h_beta].requests_served == 1
    assert by_host[h_beta].output_tokens == 200
    assert by_host[h_beta].credits_earned == Decimal("0")

    # Trust standing: alpha has a heartbeat (0.5); beta has none (None).
    assert by_host[h_alpha].reputation == 0.5
    assert by_host[h_beta].reputation is None

    # Another member's machine is never in my receipt.
    assert h_other not in by_host

    # Totals aggregate at least my machines (the live DB may add more, so assert
    # >= rather than exact equality).
    assert resp.total_requests_served >= (
        by_host[h_alpha].requests_served + by_host[h_beta].requests_served
    )
    assert resp.total_output_tokens >= (
        by_host[h_alpha].output_tokens + by_host[h_beta].output_tokens
    )
    assert resp.total_credits_earned >= by_host[h_alpha].credits_earned

    assert resp.window_days == 7
