"""0005 - Add member_keys allowlist binding members to their device keys.

Closes the member_id self-assertion hole in peer attestation. The signed-nonce
challenge already proves a peer holds the private key for its public_key; this
table lets the coordinator enforce that the key is bound to one member, so a
peer can't freely claim an arbitrary member_id.

Rows are created trust-on-first-use by default; when require_registered_keys is
set, the key must already be present (pre-seeded, or via authenticated
registration). The binding is keyed by public_key (one device key, one member).

Revision ID: 0005_member_keys
Revises: 0004_credit_escrow
Create Date: 2026-06-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_member_keys"
down_revision: str | None = "0004_credit_escrow"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "member_keys",
        sa.Column("public_key", sa.String(length=512), nullable=False),
        sa.Column("member_id", sa.UUID(), nullable=False),
        sa.Column("label", sa.String(length=256), nullable=True),
        sa.Column(
            "added_via",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'tofu'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("public_key"),
    )
    op.create_index(
        "ix_member_keys_member_id", "member_keys", ["member_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_member_keys_member_id", table_name="member_keys")
    op.drop_table("member_keys")
