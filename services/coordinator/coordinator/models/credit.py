from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base


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
