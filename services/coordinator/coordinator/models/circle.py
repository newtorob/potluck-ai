from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base


class TrustedCircle(Base):
    """A named group of members who lend each other spare compute.

    Routing scope 'circle' restricts dispatch to machines of the circle's
    other members. A member holds a CircleMembership (the owner from the
    start, everyone else by accepting an invite). Keys the owner pasted in
    with an older app live in CircleMember and still lend.
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


class CircleMembership(Base):
    """One member's place in a circle. The owner has one from creation;
    anyone else gets one by accepting an invite. Holding one lets a member
    borrow from the circle's other members, and lets them borrow from this
    member's machines that share with circles."""

    __tablename__ = "circle_memberships"
    __table_args__ = (
        UniqueConstraint("circle_id", "member_id", name="uq_circle_memberships_circle_member"),
        CheckConstraint("role IN ('owner', 'member')", name="ck_circle_memberships_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    circle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trusted_circles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CircleInvite(Base):
    """An invitation to join a circle. Only the SHA-256 of the code is kept;
    the code is shown to the owner once."""

    __tablename__ = "circle_invites"
    __table_args__ = (
        UniqueConstraint("code_hash", name="uq_circle_invites_code_hash"),
        CheckConstraint("uses >= 0 AND uses <= max_uses", name="ck_circle_invites_uses"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    circle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trusted_circles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
