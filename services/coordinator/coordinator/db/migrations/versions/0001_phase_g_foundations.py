"""0001 — Phase G foundations: contributor network + own WireGuard mesh.

Creates the schema both Phase G tracks build on:
  - m1 protocol track: peers, peer_capabilities, peer_heartbeats, jobs,
    compute_credits, credit_rates, contributor_releases
  - mesh track: households, household_members, mesh_nodes, pairing_tokens

`peers` and `compute_credits` previously lived in the api service as v0
stubs (api migration 0007). They are dropped from the api in api migration
0008 and re-created here, where they belong. No data migration — the api
stubs were never written to.

The coordinator service has no foreign-key references back to the api's
members table (services/api owns it). member_id columns are bare UUIDs
treated as opaque references; integrity is enforced at the application
layer.

Revision ID: 0001_phase_g_foundations
Revises:
Create Date: 2026-04-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_phase_g_foundations"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # peers — contributor network nodes (one per machine offering compute).
    # ------------------------------------------------------------------
    op.create_table(
        "peers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_key", sa.String(512), nullable=False),
        sa.Column("hostname", sa.String(256), nullable=True),
        sa.Column(
            "hardware_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "max_gpu_percent",
            sa.Integer(),
            server_default=sa.text("80"),
            nullable=False,
        ),
        sa.Column(
            "schedule",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_peers"),
        sa.UniqueConstraint("public_key", name="uq_peers_public_key"),
        sa.CheckConstraint(
            "max_gpu_percent BETWEEN 0 AND 100",
            name="ck_peers_max_gpu_percent",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'suspended', 'revoked')",
            name="ck_peers_status",
        ),
    )
    op.create_index("ix_peers_member_id", "peers", ["member_id"])

    # ------------------------------------------------------------------
    # compute_credits — double-entry credit ledger.
    # ------------------------------------------------------------------
    op.create_table(
        "compute_credits",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("peer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_id", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_compute_credits"),
        sa.ForeignKeyConstraint(
            ["peer_id"],
            ["peers.id"],
            name="fk_compute_credits_peer_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_compute_credits_member_id", "compute_credits", ["member_id"])
    op.create_index("ix_compute_credits_created_at", "compute_credits", ["created_at"])

    # ------------------------------------------------------------------
    # peer_capabilities — which models each peer is currently serving.
    # ------------------------------------------------------------------
    op.create_table(
        "peer_capabilities",
        sa.Column("peer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_slug", sa.String(128), nullable=False),
        sa.Column("quantization", sa.String(16), nullable=False),
        sa.Column("tokens_per_second", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("peer_id", "model_slug", name="pk_peer_capabilities"),
        sa.ForeignKeyConstraint(
            ["peer_id"],
            ["peers.id"],
            name="fk_peer_capabilities_peer_id",
            ondelete="CASCADE",
        ),
    )

    # ------------------------------------------------------------------
    # peer_heartbeats — upsert-in-place per peer on each heartbeat.
    # ------------------------------------------------------------------
    op.create_table(
        "peer_heartbeats",
        sa.Column("peer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gpu_utilization", sa.Float(), nullable=True),
        sa.Column("vram_available_gb", sa.Float(), nullable=True),
        sa.Column(
            "current_jobs",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "reputation",
            sa.Float(),
            server_default=sa.text("0.5"),
            nullable=False,
        ),
        sa.Column("binary_hash", sa.String(128), nullable=False),
        sa.Column("client_version", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("peer_id", name="pk_peer_heartbeats"),
        sa.ForeignKeyConstraint(
            ["peer_id"],
            ["peers.id"],
            name="fk_peer_heartbeats_peer_id",
            ondelete="CASCADE",
        ),
    )

    # ------------------------------------------------------------------
    # jobs — metadata only. NEVER prompt content. See manifesto §10.3.
    # ------------------------------------------------------------------
    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("peer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verify_peer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("model_slug", sa.String(128), nullable=False),
        sa.Column(
            "privacy_mode",
            sa.String(16),
            server_default=sa.text("'pseudonymous'"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("credits_earned", sa.Integer(), nullable=True),
        sa.Column("credits_spent", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.ForeignKeyConstraint(
            ["peer_id"],
            ["peers.id"],
            name="fk_jobs_peer_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["verify_peer_id"],
            ["peers.id"],
            name="fk_jobs_verify_peer_id",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "privacy_mode IN ('pseudonymous', 'mpc_2party')",
            name="ck_jobs_privacy_mode",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatched', 'streaming', 'completed', "
            "'failed', 'verified_mismatch', 'cancelled')",
            name="ck_jobs_status",
        ),
    )
    op.create_index("ix_jobs_member_id", "jobs", ["member_id"])
    op.create_index("ix_jobs_peer_id", "jobs", ["peer_id"])
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])

    # ------------------------------------------------------------------
    # credit_rates — governance-editable. New row per rate change.
    # ------------------------------------------------------------------
    op.create_table(
        "credit_rates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "earn_per_1k_output_tokens_cents",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "spend_per_1k_output_tokens_cents",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "dues_redemption_credits_per_cent",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "p_verify",
            sa.Float(),
            server_default=sa.text("0.02"),
            nullable=False,
        ),
        sa.Column(
            "effective_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "set_by_proposal_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_credit_rates"),
    )

    # ------------------------------------------------------------------
    # contributor_releases — Ed25519-signed binary hash whitelist.
    # Coordinator rejects WS handshakes whose binary_hash is not in here.
    # ------------------------------------------------------------------
    op.create_table(
        "contributor_releases",
        sa.Column("binary_hash", sa.String(128), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column(
            "signed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("signer_key", sa.String(128), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("binary_hash", name="pk_contributor_releases"),
    )

    # ------------------------------------------------------------------
    # households — a member's machine pool. Each household is a /24 slice
    # of the 10.42.0.0/16 mesh CIDR (~254 nodes per household).
    # ------------------------------------------------------------------
    op.create_table(
        "households",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("mesh_cidr", postgresql.CIDR(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_households"),
        sa.UniqueConstraint("mesh_cidr", name="uq_households_mesh_cidr"),
    )
    op.create_index("ix_households_owner_member_id", "households", ["owner_member_id"])

    # ------------------------------------------------------------------
    # household_members — many-to-many household ↔ member.
    # ------------------------------------------------------------------
    op.create_table(
        "household_members",
        sa.Column("household_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            sa.String(16),
            server_default=sa.text("'member'"),
            nullable=False,
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "household_id", "member_id", name="pk_household_members"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name="fk_household_members_household_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'member')", name="ck_household_members_role"
        ),
    )
    op.create_index(
        "ix_household_members_member_id", "household_members", ["member_id"]
    )

    # ------------------------------------------------------------------
    # mesh_nodes — one per machine joined to a household's mesh.
    # mesh_ip is allocated from the household's /24 CIDR.
    # ------------------------------------------------------------------
    op.create_table(
        "mesh_nodes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("household_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_name", sa.String(128), nullable=False),
        sa.Column("mesh_ip", postgresql.INET(), nullable=False),
        sa.Column("wg_public_key", sa.String(128), nullable=False),
        sa.Column("last_endpoint", sa.String(64), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("platform", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mesh_nodes"),
        sa.UniqueConstraint("mesh_ip", name="uq_mesh_nodes_mesh_ip"),
        sa.UniqueConstraint("wg_public_key", name="uq_mesh_nodes_wg_public_key"),
        sa.UniqueConstraint(
            "household_id", "node_name", name="uq_mesh_nodes_household_node_name"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name="fk_mesh_nodes_household_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_mesh_nodes_household_id", "mesh_nodes", ["household_id"])

    # ------------------------------------------------------------------
    # pairing_tokens — single-use, 1-hour TTL, opaque base32. DB-checked.
    # ------------------------------------------------------------------
    op.create_table(
        "pairing_tokens",
        sa.Column("token", sa.String(32), nullable=False),
        sa.Column("household_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issued_by_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consumed_by_node_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.PrimaryKeyConstraint("token", name="pk_pairing_tokens"),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name="fk_pairing_tokens_household_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["consumed_by_node_id"],
            ["mesh_nodes.id"],
            name="fk_pairing_tokens_consumed_by_node_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_pairing_tokens_household_id", "pairing_tokens", ["household_id"]
    )
    op.create_index(
        "ix_pairing_tokens_expires_at", "pairing_tokens", ["expires_at"]
    )


def downgrade() -> None:
    # Drop in reverse FK order.
    op.drop_index("ix_pairing_tokens_expires_at", table_name="pairing_tokens")
    op.drop_index("ix_pairing_tokens_household_id", table_name="pairing_tokens")
    op.drop_table("pairing_tokens")

    op.drop_index("ix_mesh_nodes_household_id", table_name="mesh_nodes")
    op.drop_table("mesh_nodes")

    op.drop_index("ix_household_members_member_id", table_name="household_members")
    op.drop_table("household_members")

    op.drop_index("ix_households_owner_member_id", table_name="households")
    op.drop_table("households")

    op.drop_table("contributor_releases")
    op.drop_table("credit_rates")

    op.drop_index("ix_jobs_created_at", table_name="jobs")
    op.drop_index("ix_jobs_peer_id", table_name="jobs")
    op.drop_index("ix_jobs_member_id", table_name="jobs")
    op.drop_table("jobs")

    op.drop_table("peer_heartbeats")
    op.drop_table("peer_capabilities")

    op.drop_index("ix_compute_credits_created_at", table_name="compute_credits")
    op.drop_index("ix_compute_credits_member_id", table_name="compute_credits")
    op.drop_table("compute_credits")

    op.drop_index("ix_peers_member_id", table_name="peers")
    op.drop_table("peers")
