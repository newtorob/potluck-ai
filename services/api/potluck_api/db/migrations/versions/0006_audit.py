"""006 - audit_log

Revision ID: 0006_audit
Revises: 0005_governance
Create Date: 2026-04-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers used by Alembic.
# ---------------------------------------------------------------------------
revision: str = "0006_audit"
down_revision: str = "0005_governance"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # Nullable: system actions (cron, webhook processors) have no actor.
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        # e.g. 'member.granted_founding', 'proposal.created', 'billing.invoice_paid'
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        # Structured context for the action; never store raw PII here.
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["members.id"],
            name="fk_audit_log_actor_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_audit_log_actor_id", "audit_log", ["actor_id"])
    # action index: allows filtering audit trail by action type without a seq scan.
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    # created_at index: chronological range queries and dashboard charts.
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_id", table_name="audit_log")
    op.drop_table("audit_log")
