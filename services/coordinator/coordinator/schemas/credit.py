"""Pydantic schemas for the credit-ledger HTTP surface."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class CreditBalanceByModel(BaseModel):
    model_slug: str
    earned: Decimal
    spent: Decimal


class CreditBalanceResponse(BaseModel):
    """Aggregated balance for the calling member.

    `earned`/`net` count only released (spendable) credits. `pending` is the sum
    of escrowed earned credits from cross-member public-pool work awaiting a
    verification result - not yet spendable.
    """

    member_id: uuid.UUID
    earned: Decimal
    spent: Decimal
    net: Decimal
    pending: Decimal = Decimal("0")
    by_model: list[CreditBalanceByModel]


class CreditHistoryEntry(BaseModel):
    """One row from the per-member credit history."""

    id: uuid.UUID
    direction: str
    job_id: uuid.UUID
    model_slug: str
    output_tokens: int
    multiplier: Decimal
    credits: Decimal
    prev_row_hash: str
    row_hash: str
    created_at: datetime


class CreditHistoryResponse(BaseModel):
    member_id: uuid.UUID
    entries: list[CreditHistoryEntry]
    next_cursor: str | None = None
