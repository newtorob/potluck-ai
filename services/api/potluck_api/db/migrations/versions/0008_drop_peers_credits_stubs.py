"""0008 - drop peers + compute_credits stubs (moving to coordinator).

The v0 stubs from migration 0007 were never written to. Phase G makes the
coordinator service the owner of all peer/job/credit tables - the api keeps
members/sessions/billing/governance, the coordinator gets peers/jobs/credits.

The coordinator's first migration (`0001_phase_g_foundations` in
services/coordinator) recreates these tables with the v1 shape.

Revision ID: 0008_drop_peers_credits_stubs
Revises: 0007_v1_stubs
Create Date: 2026-04-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_drop_peers_credits_stubs"
down_revision: str = "0007_v1_stubs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_index("ix_compute_credits_created_at", table_name="compute_credits")
    op.drop_index("ix_compute_credits_member_id", table_name="compute_credits")
    op.drop_table("compute_credits")

    op.drop_index("ix_peers_member_id", table_name="peers")
    op.drop_table("peers")


def downgrade() -> None:
    # Recreate the v0 stubs as they were in 0007. Anyone downgrading past
    # this point should also downgrade the coordinator chain to drop the v1
    # tables before bringing these stubs back, otherwise they'll collide.
    op.create_table(
        "peers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_key", sa.String(512), nullable=False),
        sa.Column(
            "hardware_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "max_gpu_percent",
            sa.Integer(),
            server_default=sa.text("80"),
            nullable=False,
        ),
        sa.Column(
            "schedule",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "contributor_mode_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_peers"),
        sa.UniqueConstraint("public_key", name="uq_peers_public_key"),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
            name="fk_peers_member_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "max_gpu_percent BETWEEN 0 AND 100",
            name="ck_peers_max_gpu_percent",
        ),
    )
    op.create_index("ix_peers_member_id", "peers", ["member_id"])

    op.create_table(
        "compute_credits",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("peer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_id", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_compute_credits"),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
            name="fk_compute_credits_member_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["peer_id"],
            ["peers.id"],
            name="fk_compute_credits_peer_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_compute_credits_member_id", "compute_credits", ["member_id"])
    op.create_index("ix_compute_credits_created_at", "compute_credits", ["created_at"])
