from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from potluck_api.db.base import Base


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint(
            "plan IN ('founding', 'monthly', 'annual')",
            name="ck_memberships_plan",
        ),
        CheckConstraint(
            "status IN ('active', 'past_due', 'canceled', 'incomplete')",
            name="ck_memberships_status",
        ),
        Index("ix_memberships_member_id_status", "member_id", "status"),
    )

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
    # "founding" | "monthly" | "annual"
    plan: Mapped[str] = mapped_column(String(32), nullable=False)
    # "active" | "past_due" | "canceled" | "incomplete"
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
