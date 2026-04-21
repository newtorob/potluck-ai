from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from potluck_api.db.base import Base


class BillingEvent(Base):
    """Immutable record of every Stripe webhook event we processed."""

    __tablename__ = "billing_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # Nullable: Stripe events can arrive before we can resolve the member.
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("members.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Stripe event ID — used as idempotency key to deduplicate retries.
    stripe_event_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    # e.g. 'checkout.session.completed', 'invoice.paid', 'customer.subscription.deleted'
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
