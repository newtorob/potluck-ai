from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base


class MemberKey(Base):
    """Allowlist binding a member to the Ed25519 device key(s) it controls.

    The WS handshake (`/peers/connect`) verifies the peer holds the private key
    for `public_key` (signed-nonce challenge), then enforces this binding so a
    key cannot freely claim an arbitrary `member_id`: a known key must attest as
    the member it is bound to. Rows are created trust-on-first-use (TOFU) by
    default, or must be pre-registered when `require_registered_keys` is set
    (strict mode for multi-member / production coordinators).

    With member auth, a signed-in member registers their device key through
    the authenticated session (added_via='session'), so first-use does not
    self-assert identity.
    """

    __tablename__ = "member_keys"

    # The peer's base64url Ed25519 public key - the binding key, one per device.
    public_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # How the binding was established: 'tofu' (first-use), 'seed' (operator SQL),
    # 'session' (authenticated registration).
    added_via: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'tofu'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
