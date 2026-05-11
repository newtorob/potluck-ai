"""0002 — Credit ledger (Phase G m4, PBC-era).

Creates the append-only ledger that records every credit event — one
'earned' row per job served (to the contributor's owner) and one 'spent'
row per job consumed (by the requester). The ledger is the single source
of truth for two downstream systems:

  (a) Member-share benefit map  — priority routing / capability tiers /
      beta access for members holding the governance share.
  (b) Contributor equity program — option grants vest on accumulated
      `earned` credits (`docs/governance/contributor-equity.md`).

This migration ALSO creates the per-model-class multiplier config table.
The `contribution_bonus_multiplier` (default 1.5 — earn pays 1.5x spend)
is held as a constant in the service layer for v0; future work moves it
to a `credit_config` singleton when governance can vote on it.

Privacy note: the ledger HAS to know member_id to attribute credit
correctly. That is allowed at this layer — it's the equivalent of REI's
membership ledger knowing which member bought what. What's NOT allowed
is forwarding member_id to a contributor peer in a JobAssignedMessage;
that invariant lives in routers/jobs.py + the privacy tests.

Cryptographic hash chain: per-member chains. Each new ledger row points
at the prev row's hash for the same member. Members can audit their own
chain to verify the coordinator hasn't retroactively edited history.

Revision ID: 0002_credit_ledger
Revises: 0001_phase_g_foundations
Create Date: 2026-05-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_credit_ledger"
down_revision: str | None = "0001_phase_g_foundations"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # credit_model_multipliers — per-model rate config. Snapshotted into
    # every ledger row at write time, so re-tuning multipliers later
    # doesn't re-price history.
    # ------------------------------------------------------------------
    op.create_table(
        "credit_model_multipliers",
        sa.Column("slug", sa.String(length=128), primary_key=True),
        sa.Column("class_label", sa.String(length=16), nullable=False),
        sa.Column("multiplier", sa.Numeric(6, 2), nullable=False),
        sa.Column("notes", sa.String(length=512), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("slug", name="pk_credit_model_multipliers"),
    )

    # Seed the table with the initial v0 tier rates. Per the design doc:
    #   ≤3B   class small   1.0
    #   7-8B  class medium  2.0
    #   13-14B class large  4.0
    #   30-34B class xlarge 8.0
    #   ≥70B  class xxlarge 16.0
    # These are proxies for GPU-seconds; refine post-launch.
    op.execute(
        """
        INSERT INTO credit_model_multipliers (slug, class_label, multiplier, notes)
        VALUES
            ('qwen2.5:3b-instruct-q4_K_M',  'small',   1.0,  '3B-class baseline'),
            ('qwen-2.5-3b-instruct-q4',     'small',   1.0,  '3B-class baseline (alt slug)'),
            ('llama-3.2-3b-instruct-q4',    'small',   1.0,  '3B-class baseline'),
            ('llama-3.1-8b-instruct-q4',    'medium',  2.0,  '7-8B class'),
            ('qwen2.5:7b-instruct-q4_K_M',  'medium',  2.0,  '7-8B class'),
            ('mistral-7b-instruct-q4',      'medium',  2.0,  '7-8B class'),
            ('llama-3.1-13b-instruct-q4',   'large',   4.0,  '13-14B class'),
            ('mixtral-8x7b-q4',             'xlarge',  8.0,  'Mixture-of-experts, ~30B active'),
            ('llama-3.1-70b-instruct-q4',   'xxlarge', 16.0, '70B+ class')
        """
    )

    # ------------------------------------------------------------------
    # credit_ledger — append-only event log.
    # Each completed job produces two rows: one 'earned' for the peer's
    # owner_member, one 'spent' for the requester. They share job_id +
    # output_tokens but their multiplier differs (earn = base × 1.5).
    # ------------------------------------------------------------------
    op.create_table(
        "credit_ledger",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "member_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        # 'earned' | 'spent'. Constraint at app layer; CHECK constraint
        # below as belt-and-suspenders.
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("model_slug", sa.String(length=128), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        # multiplier and credits snapshotted at write — re-tuning the
        # multiplier table later doesn't re-price the past.
        sa.Column("multiplier", sa.Numeric(6, 2), nullable=False),
        sa.Column("credits", sa.Numeric(12, 2), nullable=False),
        # Hash chain — per-member. Each new row's prev_row_hash is the
        # previous row's row_hash for the same member. Bootstrap value
        # for the first row in a chain is a constant ('' string-hashed).
        sa.Column("prev_row_hash", sa.String(length=64), nullable=False),
        sa.Column("row_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "direction IN ('earned','spent')", name="ck_credit_ledger_direction"
        ),
        sa.CheckConstraint(
            "output_tokens >= 0", name="ck_credit_ledger_output_tokens_nonneg"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_credit_ledger"),
    )

    op.create_index(
        "ix_credit_ledger_member_id_created_at",
        "credit_ledger",
        ["member_id", "created_at"],
    )
    op.create_index(
        "ix_credit_ledger_job_id", "credit_ledger", ["job_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_credit_ledger_job_id", table_name="credit_ledger")
    op.drop_index(
        "ix_credit_ledger_member_id_created_at", table_name="credit_ledger"
    )
    op.drop_table("credit_ledger")
    op.drop_table("credit_model_multipliers")
