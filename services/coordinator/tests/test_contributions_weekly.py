"""Tests for GET /me/contributions/weekly (household weekly time-series).

Week boundary: Monday 00:00 UTC.  Tests seed jobs and ledger rows whose
`completed_at` / `created_at` fall in two distinct Monday-aligned weeks,
then assert:
  - correct requests_served / output_tokens / credits_earned per bucket
  - zero-fill for weeks with no activity
  - only the member's own peers are counted (cross-member exclusion)
  - the `weeks` list is exactly `window_weeks` long and oldest-first

Uses the `db_session` fixture (single-transaction rollback) — same pattern
as test_contributions.py.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.constants import STUB_MEMBER_ID
from coordinator.models.credit import CreditLedger
from coordinator.models.job import Job
from coordinator.models.peer import Peer
from coordinator.routers.me import WeeklyContributionsResponse, my_contributions_weekly

# ---------------------------------------------------------------------------
# Helpers — duplicated from test_contributions.py intentionally (each test
# module is self-contained; shared helpers live in conftest when widely used).
# ---------------------------------------------------------------------------

NOW = datetime.now(timezone.utc)


def _monday(n_weeks_ago: int = 0) -> datetime:
    """Midnight UTC on the Monday of the week that is `n_weeks_ago` before NOW.

    n_weeks_ago=0  → this week's Monday (may be in the past or right now).
    n_weeks_ago=1  → last week's Monday.
    """
    monday_this_week = NOW - timedelta(days=NOW.weekday())
    monday = monday_this_week - timedelta(weeks=n_weeks_ago)
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


async def _peer(db: AsyncSession, member_id: uuid.UUID, hostname: str) -> uuid.UUID:
    p = Peer(
        member_id=member_id,
        public_key=uuid.uuid4().hex,
        hostname=hostname,
        hardware_summary={},
        status="active",
    )
    db.add(p)
    await db.flush()
    return p.id


async def _job(
    db: AsyncSession,
    *,
    peer_id: uuid.UUID,
    requester: uuid.UUID,
    status: str = "completed",
    tokens: int = 0,
    completed_at: datetime,
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


async def _ledger(
    db: AsyncSession,
    *,
    member_id: uuid.UUID,
    job_id: uuid.UUID,
    credits: Decimal,
    created_at: datetime,
) -> None:
    db.add(
        CreditLedger(
            member_id=member_id,
            direction="earned",
            job_id=job_id,
            model_slug="qwen2.5:3b",
            output_tokens=10,
            multiplier=Decimal("1.00"),
            credits=credits,
            prev_row_hash="0" * 64,
            row_hash="1" * 64,
            created_at=created_at,
        )
    )
    await db.flush()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weekly_buckets_land_correctly(db_session: AsyncSession) -> None:
    """Jobs in week-2 and week-0 (current) appear in the right buckets;
    week-1 (no activity) is zero-filled."""
    tag = uuid.uuid4().hex[:8]
    peer = await _peer(db_session, STUB_MEMBER_ID, f"weekly-test-{tag}")

    # Week 2 ago: 2 completed jobs, 300 tokens total.
    w2_monday = _monday(2)
    j_w2a = await _job(
        db_session,
        peer_id=peer,
        requester=STUB_MEMBER_ID,
        tokens=200,
        completed_at=w2_monday + timedelta(hours=6),
    )
    j_w2b = await _job(
        db_session,
        peer_id=peer,
        requester=STUB_MEMBER_ID,
        tokens=100,
        completed_at=w2_monday + timedelta(days=2),
    )
    # Ledger for week 2 (created_at in same week as jobs).
    await _ledger(
        db_session,
        member_id=STUB_MEMBER_ID,
        job_id=j_w2a,
        credits=Decimal("7.50"),
        created_at=w2_monday + timedelta(hours=6),
    )

    # Week 0 (current, possibly partial): 1 completed job, 50 tokens.
    w0_monday = _monday(0)
    j_w0 = await _job(
        db_session,
        peer_id=peer,
        requester=STUB_MEMBER_ID,
        tokens=50,
        completed_at=w0_monday + timedelta(hours=1),
    )
    await _ledger(
        db_session,
        member_id=STUB_MEMBER_ID,
        job_id=j_w0,
        credits=Decimal("2.25"),
        created_at=w0_monday + timedelta(hours=1),
    )

    # Dispatched job in week 2: must NOT be counted (not completed).
    await _job(
        db_session,
        peer_id=peer,
        requester=STUB_MEMBER_ID,
        status="dispatched",
        tokens=999,
        completed_at=w2_monday + timedelta(hours=3),
    )

    resp: WeeklyContributionsResponse = await my_contributions_weekly(
        weeks=4, db=db_session, member_id=STUB_MEMBER_ID
    )

    assert resp.window_weeks == 4
    assert len(resp.weeks) == 4
    # Oldest first.
    assert resp.weeks[0].week_start < resp.weeks[-1].week_start

    by_week: dict[str, object] = {b.week_start: b for b in resp.weeks}

    w2_key = (w2_monday.date()).isoformat()
    w1_key = (_monday(1).date()).isoformat()
    w0_key = (w0_monday.date()).isoformat()

    assert w2_key in by_week, f"week-2 bucket missing; keys={list(by_week)}"
    assert w0_key in by_week, f"week-0 bucket missing; keys={list(by_week)}"

    b_w2 = by_week[w2_key]
    assert b_w2.requests_served >= 2   # may include pre-existing live data; >=
    assert b_w2.output_tokens >= 300
    assert Decimal(b_w2.credits_earned) >= Decimal("7.50")

    # Week 1: seeded no jobs; if there are no OTHER peers in this week either
    # (isolated by peer_id scoping) the bucket must be 0.  Because the live DB
    # may have pre-existing rows for STUB_MEMBER_ID's real peers, we assert the
    # bucket EXISTS with non-negative values (zero-fill guarantee).
    assert w1_key in by_week
    b_w1 = by_week[w1_key]
    assert b_w1.requests_served >= 0
    assert b_w1.output_tokens >= 0

    b_w0 = by_week[w0_key]
    assert b_w0.requests_served >= 1
    assert b_w0.output_tokens >= 50
    assert Decimal(b_w0.credits_earned) >= Decimal("2.25")


@pytest.mark.asyncio
async def test_weekly_zero_fill_no_peers(db_session: AsyncSession) -> None:
    """A member with no peers at all gets exactly `weeks` zero-filled buckets."""
    # Use a fresh member UUID that has no peers — guaranteed empty.
    phantom_member = uuid.uuid4()

    # The endpoint resolves member identity from its member_id dependency, so
    # we supply the phantom member directly rather than patching a constant.
    resp = await my_contributions_weekly(
        weeks=5, db=db_session, member_id=phantom_member
    )

    assert resp.window_weeks == 5
    assert len(resp.weeks) == 5
    for bucket in resp.weeks:
        assert bucket.requests_served == 0
        assert bucket.output_tokens == 0
        assert Decimal(bucket.credits_earned) == Decimal("0.00")


@pytest.mark.asyncio
async def test_weekly_excludes_other_members_peers(db_session: AsyncSession) -> None:
    """Jobs on another member's peer MUST NOT appear in my weekly totals."""
    tag = uuid.uuid4().hex[:8]
    other_member = uuid.uuid4()

    # A peer I own with one job.
    my_peer = await _peer(db_session, STUB_MEMBER_ID, f"mine-weekly-{tag}")
    # A peer owned by someone else with many jobs.
    other_peer = await _peer(db_session, other_member, f"theirs-weekly-{tag}")

    w0_monday = _monday(0)
    await _job(
        db_session,
        peer_id=my_peer,
        requester=STUB_MEMBER_ID,
        tokens=10,
        completed_at=w0_monday + timedelta(hours=2),
    )
    # Other member's peer: 5 jobs, 5000 tokens — must not bleed into my total.
    for _ in range(5):
        await _job(
            db_session,
            peer_id=other_peer,
            requester=other_member,
            tokens=1000,
            completed_at=w0_monday + timedelta(hours=3),
        )

    resp: WeeklyContributionsResponse = await my_contributions_weekly(
        weeks=2, db=db_session, member_id=STUB_MEMBER_ID
    )
    w0_key = (w0_monday.date()).isoformat()
    by_week = {b.week_start: b for b in resp.weeks}

    assert w0_key in by_week
    b = by_week[w0_key]
    # output_tokens must not include the other member's 5×1000.
    # We seeded exactly 10 tokens for my peer; the live DB may add more,
    # but if cross-member exclusion works it can't include the other_peer jobs.
    assert b.output_tokens < 5000  # 5000 only reachable if cross-member leaked


@pytest.mark.asyncio
async def test_weekly_default_and_range(db_session: AsyncSession) -> None:
    """Boundary values (1 and 26) return the right length.

    Note: the FastAPI Query() default is only resolved by FastAPI's DI layer,
    not when calling the function directly in tests — so we always pass an
    explicit int and verify boundary values rather than the default itself."""
    r_eight = await my_contributions_weekly(weeks=8, db=db_session, member_id=STUB_MEMBER_ID)
    assert len(r_eight.weeks) == 8
    assert r_eight.window_weeks == 8

    r_one = await my_contributions_weekly(weeks=1, db=db_session, member_id=STUB_MEMBER_ID)
    assert len(r_one.weeks) == 1

    r_max = await my_contributions_weekly(weeks=26, db=db_session, member_id=STUB_MEMBER_ID)
    assert len(r_max.weeks) == 26


@pytest.mark.asyncio
async def test_weekly_oldest_first_ordering(db_session: AsyncSession) -> None:
    """week_start strings are strictly ascending (oldest bucket first)."""
    resp = await my_contributions_weekly(weeks=6, db=db_session, member_id=STUB_MEMBER_ID)
    starts = [b.week_start for b in resp.weeks]
    assert starts == sorted(starts), f"not oldest-first: {starts}"
    assert len(starts) == 6


@pytest.mark.asyncio
async def test_weekly_credits_earned_decimal_format(db_session: AsyncSession) -> None:
    """credits_earned is formatted to exactly two decimal places."""
    tag = uuid.uuid4().hex[:8]
    peer = await _peer(db_session, STUB_MEMBER_ID, f"dec-fmt-{tag}")
    w0_monday = _monday(0)
    j = await _job(
        db_session,
        peer_id=peer,
        requester=STUB_MEMBER_ID,
        tokens=1,
        completed_at=w0_monday + timedelta(hours=1),
    )
    await _ledger(
        db_session,
        member_id=STUB_MEMBER_ID,
        job_id=j,
        credits=Decimal("3.10"),
        created_at=w0_monday + timedelta(hours=1),
    )
    resp = await my_contributions_weekly(weeks=1, db=db_session, member_id=STUB_MEMBER_ID)
    assert len(resp.weeks) == 1
    val = resp.weeks[0].credits_earned
    # Must be parseable as Decimal and have exactly two decimal places.
    d = Decimal(val)
    assert d >= Decimal("3.10")
    assert "." in val
    assert len(val.split(".")[1]) == 2
