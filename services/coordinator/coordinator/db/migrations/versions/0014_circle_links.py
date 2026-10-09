"""0014: direct circle links.

A circle request used to be relayed through the coordinator. A circle link
lets two machines in different households talk directly: the coordinator
brokers a short-lived link, each machine's mesh sidecar adds the other as a
restricted WireGuard peer, and the request goes machine to machine.

circle_links holds one row per link. Only the SHA-256 of the link's token is
stored. A link is live while ended_at is NULL and expires_at is in the
future; one live link exists per (requester node, lender node). The row
records who linked to whom, so it is deleted 24 hours after the link ends.

Purely additive: one new table, no change to any existing table. Nothing
reads or writes it until POTLUCK_CIRCLE_LINKS_ENABLED is switched on.

Revision ID: 0014_circle_links
Revises: 0013_circle_memberships_invites
Create Date: 2026-10-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_circle_links"
down_revision: str | None = "0013_circle_memberships_invites"
branch_labels: str | None = None
depends_on: str | None = None

_END_REASONS = (
    "ended",
    "expired",
    "superseded",
    "member_left",
    "member_removed",
    "circle_deleted",
    "sharing_off",
    "node_removed",
    "lender_gone",
    "setup_failed",
    "admin",
)


def upgrade() -> None:
    op.create_table(
        "circle_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("circle_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("trusted_circles.id", ondelete="CASCADE", name="fk_circle_links_circle_id"),
                  nullable=False),
        sa.Column("requester_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requester_node_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("mesh_nodes.id", ondelete="CASCADE", name="fk_circle_links_requester_node_id"),
                  nullable=False),
        sa.Column("lender_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lender_node_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("mesh_nodes.id", ondelete="CASCADE", name="fk_circle_links_lender_node_id"),
                  nullable=False),
        sa.Column("lender_peer_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("peers.id", ondelete="SET NULL", name="fk_circle_links_lender_peer_id"),
                  nullable=True),
        sa.Column("lender_port", sa.Integer(), nullable=False),
        sa.Column("model_slug", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("renewals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(24), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_circle_links_token_hash"),
        sa.CheckConstraint("lender_port BETWEEN 1 AND 65535", name="ck_circle_links_port"),
        sa.CheckConstraint("requester_member_id <> lender_member_id",
                           name="ck_circle_links_distinct_members"),
        sa.CheckConstraint("requester_node_id <> lender_node_id",
                           name="ck_circle_links_distinct_nodes"),
        sa.CheckConstraint("expires_at > created_at", name="ck_circle_links_expiry_after_creation"),
        sa.CheckConstraint("(ended_at IS NULL) = (end_reason IS NULL)", name="ck_circle_links_end_pair"),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN (" + ", ".join(f"'{r}'" for r in _END_REASONS) + ")",
            name="ck_circle_links_end_reason",
        ),
    )

    live = sa.text("ended_at IS NULL")
    op.create_index("uq_circle_links_live_pair", "circle_links",
                    ["requester_node_id", "lender_node_id"], unique=True, postgresql_where=live)
    op.create_index("ix_circle_links_circle_live", "circle_links", ["circle_id"], postgresql_where=live)
    op.create_index("ix_circle_links_requester_node_live", "circle_links", ["requester_node_id"],
                    postgresql_where=live)
    op.create_index("ix_circle_links_lender_node_live", "circle_links", ["lender_node_id"],
                    postgresql_where=live)
    op.create_index("ix_circle_links_requester_member_live", "circle_links", ["requester_member_id"],
                    postgresql_where=live)
    op.create_index("ix_circle_links_lender_member_live", "circle_links", ["lender_member_id"],
                    postgresql_where=live)
    op.create_index("ix_circle_links_lender_peer_live", "circle_links", ["lender_peer_id"],
                    postgresql_where=live)
    op.create_index("ix_circle_links_expires_live", "circle_links", ["expires_at"], postgresql_where=live)
    op.create_index("ix_circle_links_ended_at", "circle_links", ["ended_at"],
                    postgresql_where=sa.text("ended_at IS NOT NULL"))


def downgrade() -> None:
    # Dropping the table drops its indexes and constraints with it.
    op.drop_table("circle_links")
