"""0003 — Add UNIQUE (job_id, direction) to credit_ledger.

Closes the double-spend window introduced in 0002. The streaming
finalizer in routers/jobs.py creates an asyncio.Task for
`_finalize_job_in_db`, which calls `record_job_credits`. If that task
fires twice — due to a client reconnect, coordinator restart mid-stream,
or an uncancelled duplicate task — two pairs of ledger rows can be
written silently, double-crediting both requester and contributor.

This migration adds the DB-level constraint that makes such a double
write impossible. The service layer is updated separately to use
INSERT … ON CONFLICT (job_id, direction) DO NOTHING and return the
existing rows, making `record_job_credits` a safe, idempotent no-op on
duplicate calls.

The existing index `ix_credit_ledger_job_id` is retained — it is not
superseded by the unique constraint because Postgres creates a separate
B-tree for UNIQUE but the old plain index is still useful for
non-equality range scans (e.g. "all ledger rows for a given job").

Revision ID: 0003_credit_ledger_unique
Revises: 0002_credit_ledger
Create Date: 2026-05-26
"""
from __future__ import annotations

from alembic import op

revision: str = "0003_credit_ledger_unique"
down_revision: str | None = "0002_credit_ledger"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_credit_ledger_job_id_direction",
        "credit_ledger",
        ["job_id", "direction"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_credit_ledger_job_id_direction",
        "credit_ledger",
        type_="unique",
    )
