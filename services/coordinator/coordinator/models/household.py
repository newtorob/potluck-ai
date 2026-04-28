from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import CIDR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base


class Household(Base):
    """A member's machine pool. CIDR is a /24 slice of the 10.42.0.0/16 mesh."""

    __tablename__ = "households"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    mesh_cidr: Mapped[str] = mapped_column(CIDR, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class HouseholdMember(Base):
    __tablename__ = "household_members"

    household_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("households.id", ondelete="CASCADE"),
        primary_key=True,
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'member'")
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
