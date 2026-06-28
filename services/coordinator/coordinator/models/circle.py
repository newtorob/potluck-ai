from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base


class TrustedCircle(Base):
    """A named allow-list of cooperative member pubkeys owned by one member.

    Routing scope 'circle' restricts dispatch to peers whose member's pubkey
    is in the chosen circle. Ownership is enforced server-side: every query
    gates on owner_member_id so a member never touches another's circles.
    """

    __tablename__ = "trusted_circles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CircleMember(Base):
    """One cooperative member pubkey in a trusted circle.

    member_pubkey is the Ed25519 public key string the owner sourced from
    the other member's GET /me/identity response. The coordinator validates
    format only (non-empty, bounded length); trust is the owner's to grant.

    UNIQUE(circle_id, member_pubkey) prevents duplicate entries; cascade
    delete cleans up when the circle is dropped.
    """

    __tablename__ = "circle_members"
    __table_args__ = (
        UniqueConstraint("circle_id", "member_pubkey", name="uq_circle_members_circle_pubkey"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    circle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trusted_circles.id", ondelete="CASCADE"),
        nullable=False,
    )
    member_pubkey: Mapped[str] = mapped_column(String(512), nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
