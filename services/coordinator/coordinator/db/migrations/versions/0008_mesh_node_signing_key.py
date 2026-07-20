"""0008 — mesh_nodes.signing_public_key: optional DERP-token PoP key.

Adds a nullable `signing_public_key` column to `mesh_nodes`: an OPTIONAL
Ed25519 public key (base64url, no padding) a node may register at enroll
time, distinct from `wg_public_key` (WireGuard/Curve25519 — not usable for
Ed25519 signature verification). When present, `POST /mesh/derp-token`
verifies an optional signed-nonce field against it (proof of possession);
when absent, DERP-token issuance falls back to the pre-existing
possession-of-wg_public_key-only check, so nodes enrolled before this
migration (or by a mesh client that hasn't picked up the Go-side signing
change yet) are completely unaffected. See `routers/mesh.py::issue_derp_token`
and `coordinator/models/mesh_node.py` for the full rationale.

Purely additive — no backfill, no NOT NULL, no data migration required.

Revision ID: 0008_mesh_node_signing_key
Revises: 0007_peer_quality_scores
Create Date: 2026-07-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_mesh_node_signing_key"
down_revision: str | None = "0007_peer_quality_scores"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "mesh_nodes",
        sa.Column("signing_public_key", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mesh_nodes", "signing_public_key")
