"""0004 - Add `escrowed` flag to credit_ledger for public-pool credit escrow.

Earned credits from cross-member (public-pool) jobs are held escrowed - not
counted toward the spendable balance - until a verification result releases
them (pass) or forfeits them (fail). This is the economic guard against a peer
self-reporting work it didn't do: the headline number is bounded by the
output_tokens clamp, but escrow stops unverified public-pool credits from being
redeemed at all.

`escrowed` is operational metadata and is intentionally not part of the per-row
hash chain. The chain proves credit *amounts*; escrow is mutable state the
verification path flips. Adding it does not change any existing row_hash.

Self-served / household jobs (requester member == contributor owner) are never
escrowed - they default to false.

Revision ID: 0004_credit_escrow
Revises: 0003_credit_ledger_unique
Create Date: 2026-06-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_credit_escrow"
down_revision: str | None = "0003_credit_ledger_unique"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "credit_ledger",
        sa.Column(
            "escrowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Partial index over escrowed rows so the "pending / escrowed earned credits"
    # aggregation stays cheap as the ledger grows (escrowed rows are the
    # minority and the ones the balance endpoint sums separately).
    op.create_index(
        "ix_credit_ledger_escrowed",
        "credit_ledger",
        ["member_id"],
        postgresql_where=sa.text("escrowed"),
    )


def downgrade() -> None:
    op.drop_index("ix_credit_ledger_escrowed", table_name="credit_ledger")
    op.drop_column("credit_ledger", "escrowed")
