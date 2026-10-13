"""0006 - Trusted circles: named allow-lists for 'circle' routing scope.

A TrustedCircle is owned by one member and contains a set of cooperative
member pubkeys. The routing layer restricts 'circle' scope dispatch to peers
whose member's pubkey appears in the chosen circle.

Revision ID: 0006_trusted_circles
Revises: 0005_member_keys
Create Date: 2026-06-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_trusted_circles"
down_revision: str | None = "0005_member_keys"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # trusted_circles - one named allow-list per member.
    # ------------------------------------------------------------------
    op.create_table(
        "trusted_circles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trusted_circles"),
    )
    op.create_index(
        "ix_trusted_circles_owner_member_id", "trusted_circles", ["owner_member_id"]
    )

    # ------------------------------------------------------------------
    # circle_members - per-circle member pubkey entries.
    # ------------------------------------------------------------------
    op.create_table(
        "circle_members",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("circle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_pubkey", sa.String(512), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_circle_members"),
        sa.ForeignKeyConstraint(
            ["circle_id"],
            ["trusted_circles.id"],
            name="fk_circle_members_circle_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "circle_id", "member_pubkey", name="uq_circle_members_circle_pubkey"
        ),
    )
    op.create_index("ix_circle_members_circle_id", "circle_members", ["circle_id"])


def downgrade() -> None:
    op.drop_index("ix_circle_members_circle_id", table_name="circle_members")
    op.drop_table("circle_members")
    op.drop_index("ix_trusted_circles_owner_member_id", table_name="trusted_circles")
    op.drop_table("trusted_circles")
