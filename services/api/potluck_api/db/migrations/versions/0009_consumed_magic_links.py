"""009 - consumed_magic_links (single-use magic links)

Magic links were replayable inside their 15-minute validity window: nothing
recorded that a token had already been redeemed. This table backs the
single-use enforcement added to AuthService.verify_magic_link() - the
token's `jti` claim is inserted here in the same transaction that creates
the session, and a second redemption hits a primary-key conflict.

Revision ID: 0009_consumed_magic_links
Revises: 0008_drop_peers_credits_stubs
Create Date: 2026-07-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_consumed_magic_links"
down_revision: str = "0008_drop_peers_credits_stubs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "consumed_magic_links",
        sa.Column("jti", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("jti", name="pk_consumed_magic_links"),
    )
    # Supports the (not-yet-scheduled) pruning job:
    # DELETE FROM consumed_magic_links WHERE expires_at < now()
    op.create_index(
        "ix_consumed_magic_links_expires_at",
        "consumed_magic_links",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_consumed_magic_links_expires_at", table_name="consumed_magic_links"
    )
    op.drop_table("consumed_magic_links")
