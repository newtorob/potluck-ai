"""Per-member endpoints (credit balance, history).

v0/dev: member identity comes from `_STUB_MEMBER_ID` (matches the
pattern in `routers/jobs.py` and `routers/mesh.py`). Real member auth
swaps in here when v0.5 lands.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.constants import STUB_MEMBER_ID
from coordinator.db.database import get_db
from coordinator.middleware.auth import require_coordinator_token
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

_STUB_MEMBER_ID = STUB_MEMBER_ID


@router.get(
    "/me/credit-balance",
    response_model=CreditBalanceResponse,
    dependencies=[Depends(require_coordinator_token)],
)
async def credit_balance(
    db: AsyncSession = Depends(get_db),
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
        .where(CreditLedger.member_id == _STUB_MEMBER_ID)
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
        member_id=_STUB_MEMBER_ID,
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
    dependencies=[Depends(require_coordinator_token)],
)
async def credit_history(
    direction: str | None = Query(default=None, pattern="^(earned|spent)$"),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> CreditHistoryResponse:
    """Return the calling member's ledger entries, newest first.

    Supports filter by direction. The returned `prev_row_hash` +
    `row_hash` columns let a member independently verify the integrity
    of their chain (see `docs/security/threat-model.md` §6)."""
    stmt = (
        select(CreditLedger)
        .where(CreditLedger.member_id == _STUB_MEMBER_ID)
        .order_by(CreditLedger.created_at.desc(), CreditLedger.id.desc())
        .limit(limit)
    )
    if direction is not None:
        stmt = stmt.where(CreditLedger.direction == direction)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    return CreditHistoryResponse(
        member_id=_STUB_MEMBER_ID,
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
    dependencies=[Depends(require_coordinator_token)],
)
async def my_contributions(
    days: int = Query(default=7, ge=1, le=31),
    db: AsyncSession = Depends(get_db),
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
        select(Peer.id, Peer.hostname).where(Peer.member_id == _STUB_MEMBER_ID)
    )
    peers = peers_res.all()
    my_peer_ids = [row.id for row in peers]
    if not my_peer_ids:
        return ContributionsResponse(
            member_id=_STUB_MEMBER_ID,
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
            CreditLedger.member_id == _STUB_MEMBER_ID,
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
        member_id=_STUB_MEMBER_ID,
        window_days=days,
        machines=machines,
        total_requests_served=sum(m.requests_served for m in machines),
        total_output_tokens=sum(m.output_tokens for m in machines),
        total_credits_earned=sum((m.credits_earned for m in machines), Decimal("0")),
    )
