"""0013 - circle memberships and invites.

A trusted circle starts as a list of machine keys its owner pasted in. The
person behind a key is never asked, cannot see the circle, cannot leave it,
and cannot borrow from it: only the owner can send requests.

circle_memberships makes membership a thing a member holds. The owner has
one from the start, and anyone who accepts an invite gets one. Every member
can borrow from every other member who shares, and can leave.

circle_invites holds invite codes as SHA-256 hashes; the code itself is shown
once to the owner and never stored.

circle_members (the pasted keys) is kept as it is. Released apps still write
it, and a key added that way keeps lending exactly as before.

Backfill: one owner membership per existing circle. Additive otherwise.

Revision ID: 0013_circle_memberships_invites
Revises: 0012_capability_prompt_tps
Create Date: 2026-10-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_circle_memberships_invites"
down_revision: str | None = "0012_capability_prompt_tps"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "circle_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("circle_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("trusted_circles.id", ondelete="CASCADE", name="fk_circle_memberships_circle_id"),
                  nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("circle_id", "member_id", name="uq_circle_memberships_circle_member"),
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_circle_memberships_role"),
    )
    op.create_index("ix_circle_memberships_member_id", "circle_memberships", ["member_id"])
    op.create_index("ix_circle_memberships_circle_id", "circle_memberships", ["circle_id"])

    op.create_table(
        "circle_invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("circle_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("trusted_circles.id", ondelete="CASCADE", name="fk_circle_invites_circle_id"),
                  nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("code_hash", name="uq_circle_invites_code_hash"),
        sa.CheckConstraint("uses >= 0 AND uses <= max_uses", name="ck_circle_invites_uses"),
    )
    op.create_index("ix_circle_invites_circle_id", "circle_invites", ["circle_id"])

    op.execute(
        "INSERT INTO circle_memberships (circle_id, member_id, role, joined_at) "
        "SELECT id, owner_member_id, 'owner', created_at FROM trusted_circles "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.drop_index("ix_circle_invites_circle_id", table_name="circle_invites")
    op.drop_table("circle_invites")
    op.drop_index("ix_circle_memberships_circle_id", table_name="circle_memberships")
    op.drop_index("ix_circle_memberships_member_id", table_name="circle_memberships")
    op.drop_table("circle_memberships")
