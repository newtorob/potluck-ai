"""004 - models_registry

Revision ID: 0004_models
Revises: 0003_billing
Create Date: 2026-04-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers used by Alembic.
# ---------------------------------------------------------------------------
revision: str = "0004_models"
down_revision: str = "0003_billing"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "models_registry",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # External identifier: e.g. "llama-3.1-8b-instruct-q5".
        # Unique on its own for "latest slug" lookups; composite UC below
        # enforces that the same slug can only have one row per version.
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "min_ram_gb",
            sa.Numeric(4, 1),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "min_vram_gb",
            sa.Numeric(4, 1),
            server_default=sa.text("0"),
            nullable=False,
        ),
        # 'small' | 'medium' | 'large'
        sa.Column(
            "tier",
            sa.String(32),
            server_default=sa.text("'medium'"),
            nullable=False,
        ),
        sa.Column("download_url", sa.String(512), nullable=False),
        sa.Column("manifest_signature", sa.Text(), nullable=True),
        sa.Column(
            "is_featured",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        # v1 seam: True means this model is served by contributor peers.
        sa.Column(
            "is_hosted_by_peers",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_models_registry"),
        sa.UniqueConstraint("slug", name="uq_models_registry_slug"),
        sa.UniqueConstraint("slug", "version", name="uq_models_registry_slug_version"),
        sa.CheckConstraint(
            "tier IN ('small', 'medium', 'large')",
            name="ck_models_registry_tier",
        ),
    )
    op.create_index("ix_models_registry_slug", "models_registry", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_models_registry_slug", table_name="models_registry")
    op.drop_table("models_registry")
