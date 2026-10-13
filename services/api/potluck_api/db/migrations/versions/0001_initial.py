"""001 - members + sessions

Revision ID: 0001_initial
Revises: (none)
Create Date: 2026-04-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers used by Alembic.
# ---------------------------------------------------------------------------
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # pgcrypto provides gen_random_uuid() which we use as server-side
    # default for all UUID primary keys.
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ------------------------------------------------------------------
    # members
    # ------------------------------------------------------------------
    op.create_table(
        "members",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("founding_member_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discourse_user_id", sa.Integer(), nullable=True),
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
        sa.Column(
            "is_admin",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_members"),
        sa.UniqueConstraint("email", name="uq_members_email"),
    )
    op.create_index("ix_members_email", "members", ["email"], unique=True)
    op.create_index("ix_members_stripe_customer_id", "members", ["stripe_customer_id"])

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    op.create_table(
        "sessions",
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
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
            name="fk_sessions_member_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_sessions_member_id", "sessions", ["member_id"])
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])


def downgrade() -> None:
    # Drop sessions first (references members).
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_token_hash", table_name="sessions")
    op.drop_index("ix_sessions_member_id", table_name="sessions")
    op.drop_table("sessions")

    op.drop_index("ix_members_stripe_customer_id", table_name="members")
    op.drop_index("ix_members_email", table_name="members")
    op.drop_table("members")

    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
