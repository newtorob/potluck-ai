"""0010 — peers.max_concurrent_jobs: optional per-peer concurrency cap.

Adds a nullable `max_concurrent_jobs` column to `peers`. This is the
capacity half of the pool-reliability spine (the other half is the
process-local live in-flight counter in services/peer_registry.py):
`pick_peer_for_model` HARD-EXCLUDES a peer from `eligible_ids` once its
LIVE in-flight job count reaches this cap, rather than merely dampening
its score.

Null semantics (the default-when-null contract this migration commits to):
    max_concurrent_jobs IS NULL  ->  uncapped. The peer is never removed
    from candidacy on capacity grounds; only the existing load-aware score
    term (compute / (1 + max(live_in_flight, heartbeat_current_jobs))) still
    penalises a busy peer. This is exactly today's behaviour for every row
    that predates this migration — additive and coordinated-deploy-safe, no
    backfill required.

    max_concurrent_jobs = N (N >= 0) -> the peer is excluded from
    `eligible_ids` whenever live_in_flight(peer) >= N. N=0 takes a peer out
    of new-job candidacy entirely without disconnecting it (e.g. an operator
    draining a peer for maintenance).

Placement rationale: `max_concurrent_jobs` lives on `peers` (not
`peer_heartbeats`) because it is a slow-moving, largely-static capacity fact
about the machine (how much concurrent work its operator is willing to let
it take), the same shape as the pre-existing `peers.max_gpu_percent` — not a
per-beat dynamic value like `peer_heartbeats.current_jobs`. Nothing writes
this column yet (no wire-protocol change in this migration); it is set
today only by direct operator/DB action. Wiring a peer-declared value in
through AttestationPayload (or a settings endpoint) is a deliberate
follow-on, not scope here — keeping this migration purely additive to the
schema with zero protocol surface change.

Revision ID: 0010_peer_max_concurrent_jobs
Revises: 0009_mesh_node_token
Create Date: 2026-08-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_peer_max_concurrent_jobs"
down_revision: str | None = "0009_mesh_node_token"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "peers",
        sa.Column("max_concurrent_jobs", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("peers", "max_concurrent_jobs")
