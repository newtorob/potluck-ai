"""002 — memberships

Revision ID: 0002_memberships
Revises: 0001_initial
Create Date: 2026-04-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers used by Alembic.
# ---------------------------------------------------------------------------
revision: str = "0002_memberships"
down_revision: str = "0001_initial"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "memberships",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "member_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # 'founding' | 'monthly' | 'annual'
        sa.Column("plan", sa.String(32), nullable=False),
        # 'active' | 'past_due' | 'canceled' | 'incomplete'
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memberships"),
        sa.UniqueConstraint(
            "stripe_subscription_id", name="uq_memberships_stripe_subscription_id"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
            name="fk_memberships_member_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "plan IN ('founding', 'monthly', 'annual')",
            name="ck_memberships_plan",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'past_due', 'canceled', 'incomplete')",
            name="ck_memberships_status",
        ),
    )
    op.create_index("ix_memberships_member_id", "memberships", ["member_id"])
    # Composite index for the common lookup: "find active memberships for member".
    op.create_index(
        "ix_memberships_member_id_status",
        "memberships",
        ["member_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_memberships_member_id_status", table_name="memberships")
    op.drop_index("ix_memberships_member_id", table_name="memberships")
    op.drop_table("memberships")
