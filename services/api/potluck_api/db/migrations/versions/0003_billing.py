"""003 - billing_events

Revision ID: 0003_billing
Revises: 0002_memberships
Create Date: 2026-04-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers used by Alembic.
# ---------------------------------------------------------------------------
revision: str = "0003_billing"
down_revision: str = "0002_memberships"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "billing_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # Nullable: event may arrive before we resolve a member (e.g. checkout.session.created).
        sa.Column(
            "member_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        # Stripe event ID used as idempotency key - prevents double-processing retries.
        sa.Column("stripe_event_id", sa.String(64), nullable=False),
        # e.g. 'checkout.session.completed', 'invoice.paid', 'customer.subscription.deleted'
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        # Full raw Stripe event stored for audit / replay.
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_billing_events"),
        sa.UniqueConstraint("stripe_event_id", name="uq_billing_events_stripe_event_id"),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
            name="fk_billing_events_member_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_billing_events_member_id", "billing_events", ["member_id"])
    # Index on created_at for chronological queries and cleanup jobs.
    op.create_index("ix_billing_events_created_at", "billing_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_billing_events_created_at", table_name="billing_events")
    op.drop_index("ix_billing_events_member_id", table_name="billing_events")
    op.drop_table("billing_events")
