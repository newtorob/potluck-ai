from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base

# Allowed signal_type values.  Enforced at the service layer; the column
# stores the raw string so the DB schema never needs a migration to add a
# new signal source.
VALID_SIGNAL_TYPES = frozenset({"determinism", "coherence", "latency", "user_feedback"})


class PeerQualityScore(Base):
    """One quality observation for a (peer, model) pair.

    Rows are append-only: nothing updates an existing row. The service
    layer computes a rolling weighted average over the recent window.

    Privacy: contains only routing metadata (peer_id, model_slug,
    signal_type) plus a scalar score.  No prompt, no completion, no
    requester identity — ever.
    """

    __tablename__ = "peer_quality_scores"

    # Compound index to make the sliding-window aggregate query fast: filter
    # by peer_id + model_slug, order/limit by observed_at.
    __table_args__ = (
        Index(
            "ix_peer_quality_peer_model_time",
            "peer_id",
            "model_slug",
            "observed_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    peer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("peers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(24), nullable=False)
    # 0.0 (worst) .. 1.0 (best).  The service layer clamps before insertion.
    score: Mapped[float] = mapped_column(Float, nullable=False)
    # Number of inferences this observation summarises.  Weight in the
    # aggregate is proportional to sample_size so a batch probe (sample_size=50)
    # outweighs a single-shot user rating (sample_size=1).
    sample_size: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
