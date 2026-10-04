"""0012 — peer_capabilities.prompt_tokens_per_second.

Adds a nullable prompt-processing rate next to the existing generation rate.
Until now no real peer wrote either figure (attestation stored only slug and
quantization), so the coordinator's first-token deadline always fell back
to the hardware-tier guess. The contributor now reports both from the
sidecar's benchmark; the deadline prefers this column, then
tokens_per_second, then the tier fallback.

Null semantics: NULL means "not measured", exactly today's behaviour for
every existing row. Additive, coordinated-deploy-safe, no backfill.

Revision ID: 0012_capability_prompt_tps
Revises: 0011_job_terminal_reason
Create Date: 2026-09-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012_capability_prompt_tps"
down_revision: str | None = "0011_job_terminal_reason"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "peer_capabilities",
        sa.Column("prompt_tokens_per_second", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("peer_capabilities", "prompt_tokens_per_second")
