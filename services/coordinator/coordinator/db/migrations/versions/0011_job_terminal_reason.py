"""0011: jobs.terminal_reason + jobs.error_category, one terminal outcome per job.

Adds two nullable columns to `jobs` so a settled row records HOW it settled,
not just that it settled:

    terminal_reason  -- the Outcome that finalized the row
                        (routers/jobs.py::Outcome): "completed", "timeout",
                        "cancelled", "peer_error", "dispatch_failed".
    error_category   -- the descriptive category that came with the failure,
                        when there was one. Peer-reported
                        ("inference_failed") or coordinator-synthesized
                        ("first_token_timeout", "peer_disconnected",
                        "dispatch_failed"). Diagnostic only.

Why this exists: without a single-terminal-outcome contract, some exits
would never touch the row at all (a sync timeout that raised 504 would leave
it at 'dispatched' forever; so would a failed WebSocket send), and others
would settle it as "completed" because the finalizer only inspected
error_category, which a stream timeout or a client disconnect never sets.
That would mis-record jobs and mint consumption credits for work that never
finished. Persisting the outcome makes the terminal state auditable rather
than inferred.

Null semantics (the default-when-null contract this migration commits to):
    terminal_reason IS NULL -> the row predates this migration (or predates
    the code that writes it). It says nothing about the job; read `status`.
    No backfill is attempted: there is no way to recover the real terminal
    reason for a historical row, and inventing one would be worse than NULL.

    error_category IS NULL -> either the job succeeded, or the failure
    carried no category. Same reading as terminal_reason.

Deliberately unchanged: the `jobs.status` vocabulary stays exactly
{dispatched, completed, failed}. Every aggregate in the service filters on
status == "completed" (routers/me.py contributions + earnings,
routers/transparency.py public counts, routers/jobs.py::my_job_stats), so
widening that vocabulary would silently change published numbers. The new
granularity is additive and lives beside it.

Rollout: additive, nullable, no backfill, no wire-protocol surface. Enrolled
peers never see these columns; nothing in schemas/ changes. Safe to apply
before or after the coordinator container restarts, though the coordinator
should be redeployed alongside it so the ORM model and the table agree.

Revision ID: 0011_job_terminal_reason
Revises: 0010_peer_max_concurrent_jobs
Create Date: 2026-09-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_job_terminal_reason"
down_revision: str | None = "0010_peer_max_concurrent_jobs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("terminal_reason", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("error_category", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "error_category")
    op.drop_column("jobs", "terminal_reason")
