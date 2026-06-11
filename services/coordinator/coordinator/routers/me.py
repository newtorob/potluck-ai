"""Per-member endpoints (credit balance, history).

v0/dev: member identity comes from `_STUB_MEMBER_ID` (matches the
pattern in `routers/jobs.py` and `routers/mesh.py`). Real member auth
swaps in here when v0.5 lands.
"""
from __future__ import annotations

from decimal import Decimal

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.constants import STUB_MEMBER_ID
from coordinator.db.database import get_db
from coordinator.middleware.auth import require_coordinator_token
from coordinator.models.credit import CreditLedger
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
