from __future__ import annotations

# v1 stub — compute credit ledger.
# Schema is ready for v1; the API endpoints are disabled in v0.

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from potluck_api.db.base import Base


class ComputeCredit(Base):
    """Compute credit ledger entry.

    Double-entry style: positive = earned, negative = spent.
    Schema is ready for v1 (Phase H). API endpoints are disabled in v0.
    """

    __tablename__ = "compute_credits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Positive = earned, negative = spent.
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # e.g. 'inference_served', 'redeemed_dues', 'cash_payout'
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    peer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("peers.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Opaque correlation ID from the v1 job coordinator.
    job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
