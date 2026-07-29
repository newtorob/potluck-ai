from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base


class PairingToken(Base):
    """One-time token issued to enroll a new mesh node into a household.

    Single-use, 1-hour TTL, opaque base32 encoding of 20 CSPRNG bytes
    (`secrets.token_bytes(20)` — 160 bits of entropy; see
    `routers/mesh.py::_generate_pairing_token`). Since node-token-hardening
    (v0.6) this token is no longer just a bootstrap convenience gate — a
    valid pairing token is now SUFFICIENT AUTH on its own for POST
    /mesh/enroll (no coordinator token required), so its entropy/TTL/
    single-use properties carry real security weight, not just UX
    ergonomics. All three already held before that change; verified, not
    newly hardened.
    """

    __tablename__ = "pairing_tokens"

    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    household_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("households.id", ondelete="CASCADE"),
        nullable=False,
    )
    issued_by_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_by_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mesh_nodes.id", ondelete="SET NULL"),
        nullable=True,
    )
