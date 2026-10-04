"""Short-lived CLI browser approvals.

Revision ID: 0011_device_grants
Revises: 0010_members_email_ci_unique
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0011_device_grants"
down_revision = "0010_members_email_ci_unique"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("device_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("user_code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("members.id", ondelete="CASCADE")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column("interval", sa.Integer(), nullable=False))
    op.create_index("ix_device_grants_expires_at", "device_grants", ["expires_at"])


def downgrade():
    op.drop_table("device_grants")
