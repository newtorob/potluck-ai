from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base


class ContributorRelease(Base):
    """Whitelist of Ed25519-signed binary hashes accepted by the coordinator.

    The WS handshake (`/peers/connect`) rejects any peer whose `binary_hash`
    is not in this table.
    """

    __tablename__ = "contributor_releases"

    binary_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    signer_key: Mapped[str] = mapped_column(String(128), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
