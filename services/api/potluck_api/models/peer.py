from __future__ import annotations

# v1 stub — contributor network peer node.
# Schema is ready for v1; the API endpoints are disabled in v0.

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from potluck_api.db.base import Base


class Peer(Base):
    """Contributor network peer node.

    Schema is ready for v1 (Phase G). API endpoints are disabled in v0.
    """

    __tablename__ = "peers"
    __table_args__ = (
        CheckConstraint(
            "max_gpu_percent BETWEEN 0 AND 100",
            name="ck_peers_max_gpu_percent",
        ),
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
    public_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    hardware_summary: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    max_gpu_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("80")
    )
    schedule: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    contributor_mode_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
