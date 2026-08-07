"""Per-member endpoints (credit balance, history).

Member identity comes from the caller's session via
`resolve_session_member_id` (matches the pattern in `routers/jobs.py` and
`routers/mesh.py`). In dev (`POTLUCK_SESSION_AUTH_ENABLED` unset) it resolves
to the fixture STUB_MEMBER_ID; with auth enabled it is the logged-in member.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.db.database import get_db
from coordinator.middleware.session_auth import resolve_session_member_id
from coordinator.models.credit import CreditLedger
from coordinator.models.job import Job
from coordinator.models.peer import Peer, PeerHeartbeat
from coordinator.schemas.credit import (
    CreditBalanceByModel,
    CreditBalanceResponse,
    CreditHistoryEntry,
    CreditHistoryResponse,
)

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["me"])


@router.get(
    "/me/credit-balance",
    response_model=CreditBalanceResponse,
)
async def credit_balance(
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> CreditBalanceResponse:
    """Return the calling member's aggregated credit balance.

    Aggregates from `credit_ledger`. Earned / spent / net plus a
    per-model breakdown of earned vs spent."""
    rows_result = await db.execute(
        select(
            CreditLedger.direction,
            CreditLedger.model_slug,
            CreditLedger.escrowed,
            func.sum(CreditLedger.credits).label("total"),
        )
        .where(CreditLedger.member_id == member_id)
        .group_by(
            CreditLedger.direction, CreditLedger.model_slug, CreditLedger.escrowed
        )
    )

    by_model: dict[str, dict[str, Decimal]] = {}
    earned_total = Decimal("0")  # spendable (released) earned only
    spent_total = Decimal("0")
    pending_total = Decimal("0")  # escrowed earned — not yet spendable

    for direction, model_slug, escrowed, total in rows_result.all():
        amt = Decimal(total or 0)
        # Escrowed earned credits (unverified public-pool work) are not part of
        # the spendable balance — surface them separately as `pending`.
        if direction == "earned" and escrowed:
            pending_total += amt
            continue
        by_model.setdefault(model_slug, {"earned": Decimal("0"), "spent": Decimal("0")})
        by_model[model_slug][direction] = by_model[model_slug][direction] + amt
        if direction == "earned":
            earned_total += amt
        elif direction == "spent":
            spent_total += amt

    return CreditBalanceResponse(
        member_id=member_id,
        earned=earned_total,
        spent=spent_total,
        net=earned_total - spent_total,
        pending=pending_total,
        by_model=[
            CreditBalanceByModel(
                model_slug=slug,
                earned=v["earned"],
                spent=v["spent"],
            )
            for slug, v in sorted(by_model.items())
        ],
    )


@router.get(
    "/me/credit-history",
    response_model=CreditHistoryResponse,
)
async def credit_history(
    direction: str | None = Query(default=None, pattern="^(earned|spent)$"),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> CreditHistoryResponse:
    """Return the calling member's ledger entries, newest first.

    Supports filter by direction. The returned `prev_row_hash` +
    `row_hash` columns let a member independently verify the integrity
    of their chain (see `docs/security/threat-model.md` §6)."""
    stmt = (
        select(CreditLedger)
        .where(CreditLedger.member_id == member_id)
        .order_by(CreditLedger.created_at.desc(), CreditLedger.id.desc())
        .limit(limit)
    )
    if direction is not None:
        stmt = stmt.where(CreditLedger.direction == direction)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    return CreditHistoryResponse(
        member_id=member_id,
        entries=[
            CreditHistoryEntry(
                id=row.id,
                direction=row.direction,
                job_id=row.job_id,
                model_slug=row.model_slug,
                output_tokens=row.output_tokens,
                multiplier=row.multiplier,
                credits=row.credits,
                prev_row_hash=row.prev_row_hash,
                row_hash=row.row_hash,
                created_at=row.created_at,
            )
            for row in rows
        ],
    )


# ---------------------------------------------------------------------------
# Contribution receipts — per-machine weekly "what my machines did"
# ---------------------------------------------------------------------------


class ContributionMachine(BaseModel):
    """One of the member's own machines and what it did this window."""

    peer_id: str
    hostname: str | None
    requests_served: int
    output_tokens: int
    credits_earned: Decimal
    # Canary trust score in [0,1] (neutral 0.5; 0.0 = flagged). None when the
    # machine has no heartbeat row yet. The member's own machine only.
    reputation: float | None


class ContributionsResponse(BaseModel):
    member_id: uuid.UUID
    window_days: int
    machines: list[ContributionMachine]
    total_requests_served: int
    total_output_tokens: int
    total_credits_earned: Decimal


@router.get(
    "/me/contributions",
    response_model=ContributionsResponse,
)
async def my_contributions(
    days: int = Query(default=7, ge=1, le=31),
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> ContributionsResponse:
    """Per-machine contribution receipt for the calling member, over the last
    `days`. For each machine the member OWNS: requests served (completed jobs the
    machine ran) and credits earned (cross-member ledger; 0 for self-served /
    household work). Only the member's own machines appear, never another
    member's data."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    # The member's own machines.
    peers_res = await db.execute(
        select(Peer.id, Peer.hostname).where(Peer.member_id == member_id)
    )
    peers = peers_res.all()
    my_peer_ids = [row.id for row in peers]
    if not my_peer_ids:
        return ContributionsResponse(
            member_id=member_id,
            window_days=days,
            machines=[],
            total_requests_served=0,
            total_output_tokens=0,
            total_credits_earned=Decimal("0"),
        )

    # Requests served + tokens per machine (jobs the machine completed).
    jobs_res = await db.execute(
        select(
            Job.peer_id,
            func.count().label("n"),
            func.coalesce(func.sum(Job.output_tokens), 0).label("tokens"),
        )
        .where(
            Job.peer_id.in_(my_peer_ids),
            Job.status == "completed",
            Job.completed_at > since,
        )
        .group_by(Job.peer_id)
    )
    served: dict[uuid.UUID, tuple[int, int]] = {
        row.peer_id: (int(row.n), int(row.tokens)) for row in jobs_res.all()
    }

    # Credits earned per machine: earned ledger rows joined to the job's peer.
    # (The ledger has no peer column; it attributes via job_id -> jobs.peer_id,
    # the same join release_escrow_for_peer uses.)
    credits_res = await db.execute(
        select(Job.peer_id, func.coalesce(func.sum(CreditLedger.credits), 0).label("c"))
        .select_from(CreditLedger)
        .join(Job, CreditLedger.job_id == Job.id)
        .where(
            CreditLedger.member_id == member_id,
            CreditLedger.direction == "earned",
            CreditLedger.created_at > since,
            Job.peer_id.in_(my_peer_ids),
        )
        .group_by(Job.peer_id)
    )
    earned: dict[uuid.UUID, Decimal] = {
        row.peer_id: Decimal(row.c or 0) for row in credits_res.all()
    }

    # Trust standing per machine (canary reputation). Own peers only.
    rep_res = await db.execute(
        select(PeerHeartbeat.peer_id, PeerHeartbeat.reputation).where(
            PeerHeartbeat.peer_id.in_(my_peer_ids)
        )
    )
    reputations: dict[uuid.UUID, float] = {
        row.peer_id: float(row.reputation) for row in rep_res.all()
    }

    machines: list[ContributionMachine] = []
    for row in peers:
        n, tokens = served.get(row.id, (0, 0))
        credits = earned.get(row.id, Decimal("0"))
        # Keep the receipt tight: only machines that did something this window.
        if n == 0 and credits == 0:
            continue
        machines.append(
            ContributionMachine(
                peer_id=str(row.id),
                hostname=row.hostname,
                requests_served=n,
                output_tokens=tokens,
                credits_earned=credits,
                reputation=reputations.get(row.id),
            )
        )

    machines.sort(key=lambda m: (m.requests_served, m.output_tokens), reverse=True)
    return ContributionsResponse(
        member_id=member_id,
        window_days=days,
        machines=machines,
        total_requests_served=sum(m.requests_served for m in machines),
        total_output_tokens=sum(m.output_tokens for m in machines),
        total_credits_earned=sum((m.credits_earned for m in machines), Decimal("0")),
    )


# ---------------------------------------------------------------------------
# Weekly contribution time-series — household-wide bar-chart data
# ---------------------------------------------------------------------------
# Week boundary: Monday 00:00 UTC (ISO week convention).
# date_trunc('week', ...) in Postgres uses Monday as the start of the week
# when the session timezone is UTC — confirmed by PostgreSQL docs §9.9.
# ---------------------------------------------------------------------------


class WeeklyContributionBucket(BaseModel):
    """Household-wide contribution aggregate for one UTC week bucket."""

    # ISO date string for the Monday that opens this week (YYYY-MM-DD).
    week_start: str
    requests_served: int
    output_tokens: int
    # Decimal serialised as a string to preserve two decimal places across
    # JSON without floating-point rounding (mirrors CreditHistoryEntry.credits).
    credits_earned: str


class WeeklyContributionsResponse(BaseModel):
    member_id: uuid.UUID
    window_weeks: int
    # Ordered oldest first, length exactly window_weeks.
    weeks: list[WeeklyContributionBucket]


def _week_start_utc(n_weeks_ago: int, now: datetime) -> date:
    """Monday 00:00 UTC for the week that is `n_weeks_ago` before `now`.

    n_weeks_ago=0  → the current (possibly partial) week.
    n_weeks_ago=1  → last week, etc.
    """
    # weekday(): Monday=0 … Sunday=6
    monday_this_week = (now - timedelta(days=now.weekday())).date()
    return monday_this_week - timedelta(weeks=n_weeks_ago)


@router.get(
    "/me/contributions/weekly",
    response_model=WeeklyContributionsResponse,
)
async def my_contributions_weekly(
    weeks: int = Query(default=8, ge=1, le=26),
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> WeeklyContributionsResponse:
    """Household-wide weekly contribution time-series for the calling member.

    Returns the last `weeks` UTC week buckets (Monday 00:00 UTC boundary),
    ordered oldest first.  The current partial week is always included as
    the final bucket.  Weeks with no activity are zero-filled — the response
    always has exactly `weeks` entries.

    Aggregates ACROSS all of the member's own machines; never exposes
    per-machine identity, prompt content, or requester identity."""
    now = datetime.now(timezone.utc)

    # Build the ordered list of week-start dates: oldest first.
    week_starts: list[date] = [
        _week_start_utc(i, now) for i in range(weeks - 1, -1, -1)
    ]
    # The overall window begins at the start of the oldest week.
    window_start = datetime(
        week_starts[0].year, week_starts[0].month, week_starts[0].day,
        tzinfo=timezone.utc,
    )

    # The member's own peer IDs — the scope fence that ensures own-only data.
    peers_res = await db.execute(
        select(Peer.id).where(Peer.member_id == member_id)
    )
    my_peer_ids = [row.id for row in peers_res.all()]

    if not my_peer_ids:
        return WeeklyContributionsResponse(
            member_id=member_id,
            window_weeks=weeks,
            weeks=[
                WeeklyContributionBucket(
                    week_start=ws.isoformat(),
                    requests_served=0,
                    output_tokens=0,
                    credits_earned="0.00",
                )
                for ws in week_starts
            ],
        )

    # --- Jobs: requests_served + output_tokens bucketed by completed_at week ---
    # Selects ONLY aggregate counts/sums; no prompt content, no requester
    # identity, no per-request rows — peer_id is used as the JOIN key to
    # scope to own machines and is not included in the SELECT list.
    jobs_res = await db.execute(
        select(
            func.date_trunc("week", Job.completed_at).label("bucket"),
            func.count().label("n"),
            func.coalesce(func.sum(Job.output_tokens), 0).label("tokens"),
        )
        .where(
            Job.peer_id.in_(my_peer_ids),
            Job.status == "completed",
            Job.completed_at >= window_start,
        )
        .group_by(text("1"))
    )
    # Map truncated-to-Monday datetime → (count, tokens).
    jobs_by_week: dict[date, tuple[int, int]] = {}
    for row in jobs_res.all():
        bucket_date = row.bucket.date() if row.bucket else None
        if bucket_date is not None:
            jobs_by_week[bucket_date] = (int(row.n), int(row.tokens))

    # --- Credits earned: bucketed by ledger created_at week ---
    # Same scope: member's own peers via job_id -> Job.peer_id join.
    # Selects ONLY aggregate sums; no per-row data.
    credits_res = await db.execute(
        select(
            func.date_trunc("week", CreditLedger.created_at).label("bucket"),
            func.coalesce(func.sum(CreditLedger.credits), 0).label("c"),
        )
        .select_from(CreditLedger)
        .join(Job, CreditLedger.job_id == Job.id)
        .where(
            CreditLedger.member_id == member_id,
            CreditLedger.direction == "earned",
            CreditLedger.created_at >= window_start,
            Job.peer_id.in_(my_peer_ids),
        )
        .group_by(text("1"))
    )
    credits_by_week: dict[date, Decimal] = {}
    for row in credits_res.all():
        bucket_date = row.bucket.date() if row.bucket else None
        if bucket_date is not None:
            credits_by_week[bucket_date] = Decimal(row.c or 0)

    # Zero-fill: iterate the pre-built week_starts list to produce exactly
    # `weeks` buckets in oldest-first order.
    result_weeks: list[WeeklyContributionBucket] = []
    for ws in week_starts:
        n, tokens = jobs_by_week.get(ws, (0, 0))
        c = credits_by_week.get(ws, Decimal("0"))
        result_weeks.append(
            WeeklyContributionBucket(
                week_start=ws.isoformat(),
                requests_served=n,
                output_tokens=tokens,
                credits_earned=f"{c:.2f}",
            )
        )

    return WeeklyContributionsResponse(
        member_id=member_id,
        window_weeks=weeks,
        weeks=result_weeks,
    )
