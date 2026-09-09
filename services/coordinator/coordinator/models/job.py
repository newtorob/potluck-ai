from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base


class Job(Base):
    """Job metadata. Prompt content is NEVER stored — see manifesto §10.3."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    peer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("peers.id", ondelete="SET NULL"),
        nullable=True,
    )
    verify_peer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("peers.id", ondelete="SET NULL"),
        nullable=True,
    )
    model_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    privacy_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pseudonymous'")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    # Coarse lifecycle vocabulary, unchanged: dispatched | completed | failed.
    # Every aggregate query in the service filters on status == "completed"
    # (routers/me.py, routers/transparency.py, routers/jobs.py::my_job_stats),
    # so the vocabulary is frozen and the granular reason lives below.
    #
    # terminal_reason: which Outcome settled this row
    # (routers/jobs.py::Outcome -- completed | timeout | cancelled |
    # peer_error | dispatch_failed). Nullable with no backfill: rows written
    # before migration 0011 keep NULL, which reads as "unknown, predates the
    # single-terminal-outcome contract".
    terminal_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The descriptive category that came with the failure, when there was
    # one. Peer-reported ("inference_failed") or coordinator-synthesized
    # ("first_token_timeout", "peer_disconnected", "dispatch_failed").
    # Diagnostic only: nothing routes, scores, or bills off this column.
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credits_earned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credits_spent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
