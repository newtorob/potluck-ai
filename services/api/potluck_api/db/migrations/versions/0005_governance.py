"""005 — proposals + votes

Revision ID: 0005_governance
Revises: 0004_models
Create Date: 2026-04-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers used by Alembic.
# ---------------------------------------------------------------------------
revision: str = "0005_governance"
down_revision: str = "0004_models"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # proposals
    # ------------------------------------------------------------------
    op.create_table(
        "proposals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # Discourse topic ID for read-through syncing. NULL until Discourse-synced.
        sa.Column("discourse_topic_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        # 'open' | 'closed' | 'passed' | 'rejected'
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
        # Optional FK to the creating member; nullable for system-created proposals.
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proposals"),
        sa.UniqueConstraint("discourse_topic_id", name="uq_proposals_discourse_topic_id"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["members.id"],
            name="fk_proposals_created_by",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'closed', 'passed', 'rejected')",
            name="ck_proposals_status",
        ),
    )
    # Index on closes_at so closing-job queries are fast.
    op.create_index("ix_proposals_closes_at", "proposals", ["closes_at"])

    # ------------------------------------------------------------------
    # votes
    # ------------------------------------------------------------------
    op.create_table(
        "votes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "proposal_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "member_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # 'yes' | 'no' | 'abstain'
        sa.Column("choice", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_votes"),
        # One vote per member per proposal — enforced here and via ON CONFLICT in upserts.
        sa.UniqueConstraint(
            "proposal_id", "member_id", name="uq_votes_proposal_member"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["proposals.id"],
            name="fk_votes_proposal_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
            name="fk_votes_member_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "choice IN ('yes', 'no', 'abstain')",
            name="ck_votes_choice",
        ),
    )
    op.create_index("ix_votes_proposal_id", "votes", ["proposal_id"])
    op.create_index("ix_votes_member_id", "votes", ["member_id"])


def downgrade() -> None:
    # Drop votes first (references proposals).
    op.drop_index("ix_votes_member_id", table_name="votes")
    op.drop_index("ix_votes_proposal_id", table_name="votes")
    op.drop_table("votes")

    op.drop_index("ix_proposals_closes_at", table_name="proposals")
    op.drop_table("proposals")
