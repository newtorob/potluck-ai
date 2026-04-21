"""007 — peers + compute_credits (v1 stubs)

Schema is created in v0 so that:
  - Future v1 migrations can ALTER TABLE rather than CREATE TABLE (safer).
  - The ORM models are importable and testable without a schema migration at v1 launch.

API endpoints for both tables are DISABLED in v0 and guarded by a feature flag.

Revision ID: 0007_v1_stubs
Revises: 0006_audit
Create Date: 2026-04-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers used by Alembic.
# ---------------------------------------------------------------------------
revision: str = "0007_v1_stubs"
down_revision: str = "0006_audit"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # peers — contributor network peer nodes.
    # ------------------------------------------------------------------
    op.create_table(
        "peers",
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
        # Ed25519 public key (base64-encoded). Used for job signing in v1.
        sa.Column("public_key", sa.String(512), nullable=False),
        # GPU model, VRAM, CPU, RAM — opaque dict, indexed later if needed.
        sa.Column(
            "hardware_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        # Maximum GPU utilisation the member allows the coordinator to consume.
        sa.Column(
            "max_gpu_percent",
            sa.Integer(),
            server_default=sa.text("80"),
            nullable=False,
        ),
        # Optional availability schedule (e.g. {"weekdays": "22:00-06:00"}).
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

    # ------------------------------------------------------------------
    # compute_credits — double-entry credit ledger.
    # Positive amount_cents = earned; negative = spent.
    # ------------------------------------------------------------------
    op.create_table(
        "compute_credits",
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
        # Signed: positive = earned (inference served), negative = spent (dues/payout).
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        # e.g. 'inference_served', 'redeemed_dues', 'cash_payout'
        sa.Column("reason", sa.String(64), nullable=False),
        # Link to the peer that earned this credit (NULL for non-peer entries).
        sa.Column(
            "peer_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        # Opaque correlation ID from the v1 job coordinator; used for reconciliation.
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


def downgrade() -> None:
    # Drop compute_credits first (references peers).
    op.drop_index("ix_compute_credits_created_at", table_name="compute_credits")
    op.drop_index("ix_compute_credits_member_id", table_name="compute_credits")
    op.drop_table("compute_credits")

    op.drop_index("ix_peers_member_id", table_name="peers")
    op.drop_table("peers")
