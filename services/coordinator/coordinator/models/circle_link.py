from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base

# Why a link ended. The signal message to the sidecars folds these into four
# words (see services/circle_links.py), but the row keeps the exact cause.
END_REASONS = (
    "ended",
    "expired",
    "superseded",
    "member_left",
    "member_removed",
    "circle_deleted",
    "sharing_off",
    "node_removed",
    "lender_gone",
    "setup_failed",
    "admin",
)


class CircleLink(Base):
    """A short-lived link between two machines in different households, set up
    so a circle request can go machine to machine instead of through the
    coordinator.

    Live means ended_at IS NULL and expires_at is in the future; there is no
    status column. Only the SHA-256 of the link's token is stored. One live
    link exists per (requester node, lender node); a repeat create for the
    same pair ends the old row as superseded.

    The row records who linked to whom, so it is deleted 24 hours after it
    ends. Deleting a circle, a machine or its household deletes its rows at
    once (ON DELETE CASCADE); the revocation hooks end links first so the
    sidecars hear about it.
    """

    __tablename__ = "circle_links"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_circle_links_token_hash"),
        CheckConstraint("lender_port BETWEEN 1 AND 65535", name="ck_circle_links_port"),
        CheckConstraint(
            "requester_member_id <> lender_member_id", name="ck_circle_links_distinct_members"
        ),
        CheckConstraint(
            "requester_node_id <> lender_node_id", name="ck_circle_links_distinct_nodes"
        ),
        CheckConstraint("expires_at > created_at", name="ck_circle_links_expiry_after_creation"),
        CheckConstraint(
            "(ended_at IS NULL) = (end_reason IS NULL)", name="ck_circle_links_end_pair"
        ),
        CheckConstraint(
            "end_reason IS NULL OR end_reason IN ("
            + ", ".join(f"'{r}'" for r in END_REASONS)
            + ")",
            name="ck_circle_links_end_reason",
        ),
        Index(
            "uq_circle_links_live_pair",
            "requester_node_id",
            "lender_node_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_circle_links_circle_live", "circle_id", postgresql_where=text("ended_at IS NULL")
        ),
        Index(
            "ix_circle_links_requester_node_live",
            "requester_node_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_circle_links_lender_node_live",
            "lender_node_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_circle_links_requester_member_live",
            "requester_member_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_circle_links_lender_member_live",
            "lender_member_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_circle_links_lender_peer_live",
            "lender_peer_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index("ix_circle_links_expires_live", "expires_at", postgresql_where=text("ended_at IS NULL")),
        Index(
            "ix_circle_links_ended_at", "ended_at", postgresql_where=text("ended_at IS NOT NULL")
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    circle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trusted_circles.id", ondelete="CASCADE", name="fk_circle_links_circle_id"),
        nullable=False,
    )
    # Members are plain ids: the members table belongs to the account service.
    requester_member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requester_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mesh_nodes.id", ondelete="CASCADE", name="fk_circle_links_requester_node_id"),
        nullable=False,
    )
    lender_member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lender_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mesh_nodes.id", ondelete="CASCADE", name="fk_circle_links_lender_node_id"),
        nullable=False,
    )
    # The engine connection that answers on the lender node, so a disconnect or
    # a change of sharing can find its links. NULL once that peer row is gone.
    lender_peer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("peers.id", ondelete="SET NULL", name="fk_circle_links_lender_peer_id"),
        nullable=True,
    )
    lender_port: Mapped[int] = mapped_column(Integer, nullable=False)
    model_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    # Hex SHA-256 of the token text. The token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    renewals: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
