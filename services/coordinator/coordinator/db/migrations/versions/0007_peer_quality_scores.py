"""0007 - Peer quality scores: per-(peer, model) quality observation log.

Each row is one quality signal (determinism probe, coherence probe, latency
measurement, or user feedback) for a specific peer and model slug. The service
layer aggregates these into a rolling weighted score used by the dispatch path
to deprioritise or hard-exclude persistently bad peers.

Revision ID: 0007_peer_quality_scores
Revises: 0006_trusted_circles
Create Date: 2026-06-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_peer_quality_scores"
down_revision: str | None = "0006_trusted_circles"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "peer_quality_scores",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("peer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_slug", sa.String(128), nullable=False),
        # One of: 'determinism' | 'coherence' | 'latency' | 'user_feedback'
        sa.Column("signal_type", sa.String(24), nullable=False),
        # 0.0 (worst quality observed) .. 1.0 (best).
        sa.Column("score", sa.Float, nullable=False),
        # Number of inferences summarised in this observation; weight in the
        # rolling aggregate is proportional to sample_size.
        sa.Column(
            "sample_size",
            sa.Integer,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_peer_quality_scores"),
        sa.ForeignKeyConstraint(
            ["peer_id"],
            ["peers.id"],
            name="fk_peer_quality_scores_peer_id",
            ondelete="CASCADE",
        ),
    )
    # Separate single-column index on peer_id (for FK lookups / cascade checks).
    op.create_index(
        "ix_peer_quality_scores_peer_id",
        "peer_quality_scores",
        ["peer_id"],
    )
    # Compound index: sliding-window aggregate queries filter on peer_id +
    # model_slug and order by observed_at.
    op.create_index(
        "ix_peer_quality_peer_model_time",
        "peer_quality_scores",
        ["peer_id", "model_slug", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_peer_quality_peer_model_time", table_name="peer_quality_scores")
    op.drop_index("ix_peer_quality_scores_peer_id", table_name="peer_quality_scores")
    op.drop_table("peer_quality_scores")
