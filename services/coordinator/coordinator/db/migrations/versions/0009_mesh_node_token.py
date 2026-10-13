"""0009 - mesh_nodes.node_token_hash: per-node bearer credential.

Adds a nullable, unique `node_token_hash` column to `mesh_nodes`: the
SHA-256 hex digest of a per-node bearer token ("node_token") minted once at
a successful `POST /mesh/enroll` and returned to the caller exactly then
(the coordinator never stores or logs the raw value). This lets an
app-spawned mesh sidecar authorize its own ongoing calls (heartbeat, peers,
derp-token, node delete, the `/mesh/signal` WS upgrade) without ever needing
the machine-wide `POTLUCK_COORDINATOR_TOKEN` - see
`coordinator/middleware/node_auth.py::resolve_node_from_token`.

Purely additive - no backfill, no NOT NULL, no data migration required.
Nodes enrolled without a node token simply have `node_token_hash = NULL` and
every machine route keeps accepting the legacy coordinator token
(back-compat).

Revision ID: 0009_mesh_node_token
Revises: 0008_mesh_node_signing_key
Create Date: 2026-07-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_mesh_node_token"
down_revision: str | None = "0008_mesh_node_signing_key"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "mesh_nodes",
        sa.Column("node_token_hash", sa.String(64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_mesh_nodes_node_token_hash", "mesh_nodes", ["node_token_hash"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_mesh_nodes_node_token_hash", "mesh_nodes", type_="unique"
    )
    op.drop_column("mesh_nodes", "node_token_hash")
