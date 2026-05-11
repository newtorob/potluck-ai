from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base


# ---------------------------------------------------------------------------
# Legacy v0 tables — never written to anywhere. Preserved so migration
# 0001 doesn't fail; will be removed in a future cleanup migration once
# we're confident nothing else references them.
# ---------------------------------------------------------------------------


class ComputeCredit(Base):
    __tablename__ = "compute_credits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    peer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("peers.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CreditRate(Base):
    __tablename__ = "credit_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    earn_per_1k_output_tokens_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    spend_per_1k_output_tokens_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    dues_redemption_credits_per_cent: Mapped[int] = mapped_column(Integer, nullable=False)
    p_verify: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.02")
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    set_by_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )


# ---------------------------------------------------------------------------
# Phase G m4 — credit ledger (PBC-era, non-monetary).
# ---------------------------------------------------------------------------


class CreditModelMultiplier(Base):
    """Per-model rate config. Snapshotted into every ledger row at write
    time so retuning multipliers later doesn't re-price history."""

    __tablename__ = "credit_model_multipliers"

    slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    class_label: Mapped[str] = mapped_column(String(16), nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CreditLedger(Base):
    """Append-only per-job credit event. One 'earned' row per job served,
    one 'spent' row per job consumed. Hash-chained per-member so the
    coordinator can't retroactively edit history without breaking the
    chain."""

    __tablename__ = "credit_ledger"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('earned','spent')", name="ck_credit_ledger_direction"
        ),
        CheckConstraint(
            "output_tokens >= 0",
            name="ck_credit_ledger_output_tokens_nonneg",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    credits: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    prev_row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
