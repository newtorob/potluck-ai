"""Mutations on peer_heartbeats + peers triggered by post-handshake messages.

Kept separate from attestation.py because attestation is a one-shot
verification step; this module is called per heartbeat (i.e. on a hot
path, repeated every 30s per connected peer).
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models import Peer, PeerHeartbeat
from coordinator.schemas.peer import HeartbeatMessage

logger = structlog.get_logger(__name__)


async def apply_heartbeat(
    db: AsyncSession,
    hb: HeartbeatMessage,
    binary_hash: str,
    client_version: str,
) -> None:
    """Upsert peer_heartbeats with the values the peer reported, bump
    peers.last_seen_at. Caller is responsible for the surrounding
    transaction (commit/rollback).

    `binary_hash` and `client_version` are passed in from the session
    (captured at handshake) rather than trusting whatever the peer puts
    in the heartbeat envelope - they don't change mid-session, and
    revalidating them per-heartbeat would just be churn.
    """
    now = datetime.now(timezone.utc)

    hb_stmt = (
        pg_insert(PeerHeartbeat)
        .values(
            peer_id=hb.peer_id,
            last_beat_at=now,
            current_jobs=hb.current_jobs,
            gpu_utilization=hb.gpu_utilization,
            vram_available_gb=hb.vram_available_gb,
            binary_hash=binary_hash,
            client_version=client_version,
        )
        .on_conflict_do_update(
            index_elements=["peer_id"],
            set_={
                "last_beat_at": now,
                "current_jobs": hb.current_jobs,
                "gpu_utilization": hb.gpu_utilization,
                "vram_available_gb": hb.vram_available_gb,
            },
        )
    )
    await db.execute(hb_stmt)

    peer_values: dict = {"last_seen_at": now}
    if hb.max_concurrent_jobs is not None:
        # The peer's own cap. pick_peer_for_model hard-excludes the peer once
        # its live in-flight count reaches it (see migration 0010).
        peer_values["max_concurrent_jobs"] = hb.max_concurrent_jobs
    await db.execute(
        update(Peer).where(Peer.id == hb.peer_id).values(**peer_values)
    )
