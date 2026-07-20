from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from potluck_api.db.base import Base


class ConsumedMagicLink(Base):
    """Marks a magic-link JWT (by its ``jti`` claim) as already redeemed.

    AuthService.verify_magic_link() inserts a row here in the same
    transaction that creates the session. A second redemption of the same
    token hits a primary-key conflict on ``jti`` and the whole transaction
    (session included) rolls back — this is what makes magic links single-use.

    ``expires_at`` mirrors the JWT's own ``exp`` claim. Once a row's
    ``expires_at`` has passed, the token would fail JWT expiry verification
    on its own, so the row is safe to delete.

    TODO: no pruning job runs yet — this table grows by one row per redeemed
    magic link forever. Add a periodic
    ``DELETE FROM consumed_magic_links WHERE expires_at < now()`` (cron /
    scheduled task) once there's an ops story for periodic jobs in this
    service.
    """

    __tablename__ = "consumed_magic_links"

    jti: Mapped[str] = mapped_column(String(36), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
