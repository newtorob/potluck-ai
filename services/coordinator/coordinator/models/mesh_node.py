from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base


class MeshNode(Base):
    """One row per machine joined to a household's WireGuard mesh.

    `mesh_ip` is allocated from the parent household's /24 CIDR.
    """

    __tablename__ = "mesh_nodes"
    __table_args__ = (
        UniqueConstraint(
            "household_id", "node_name", name="uq_mesh_nodes_household_node_name"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    household_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("households.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_name: Mapped[str] = mapped_column(String(128), nullable=False)
    mesh_ip: Mapped[str] = mapped_column(INET, nullable=False, unique=True)
    wg_public_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # OPTIONAL Ed25519 key (base64url, no padding) used only for the
    # DERP-token proof-of-possession challenge (POST /mesh/derp-token). This
    # is deliberately NOT the wg_public_key: WireGuard keys are Curve25519,
    # used only for the WG DH handshake, and are not usable for Ed25519
    # signature verification — so PoP needs a distinct signing key. Nullable
    # and additive: nodes enrolled before this shipped (or by a mesh client
    # that hasn't been updated yet) simply have none, and DERP-token issuance
    # falls back to the legacy wg_public_key-possession-only check (see
    # `routers/mesh.py::issue_derp_token`). Set at enrollment time
    # (`EnrollRequest.signing_public_key`, optional) — there is no separate
    # rotation endpoint yet (matches the WG key's own "post-launch" rotation
    # note above).
    signing_public_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_endpoint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
