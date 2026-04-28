"""Public-facing fleet aggregates. No member_id, no peer_id, no
individual peer details — only counts.

This endpoint is intentionally low-fidelity: it should be safe to expose
without authentication, and informative enough that a member or
prospective member can verify the network is alive and roughly
characterize who's contributing (platform mix, version distribution).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.db.database import get_db
from coordinator.models import ContributorRelease, Peer, PeerHeartbeat

router = APIRouter(tags=["transparency"])

ONLINE_WINDOW_MINUTES = 5


@router.get("/transparency/fleet")
async def fleet(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Aggregate fleet statistics. Public, no auth, no individual peers."""
    now = datetime.now(timezone.utc)
    online_threshold = now - timedelta(minutes=ONLINE_WINDOW_MINUTES)

    total_active = await db.scalar(
        select(func.count(Peer.id)).where(Peer.status == "active")
    )
    online = await db.scalar(
        select(func.count(Peer.id)).where(
            Peer.status == "active", Peer.last_seen_at > online_threshold
        )
    )

    # Platform mix from peers.hardware_summary->>'system' (lowercase).
    # Falls back to "unknown" for older rows missing the field.
    platform_rows = (
        await db.execute(
            select(
                func.coalesce(
                    func.lower(Peer.hardware_summary["system"].astext),
                    "unknown",
                ).label("platform"),
                func.count(Peer.id).label("count"),
            )
            .where(Peer.status == "active")
            .group_by("platform")
        )
    ).all()
    platforms = {row.platform: row.count for row in platform_rows}

    # Version distribution from peer_heartbeats. Only counts peers that
    # have heartbeated at least once (which is true after any successful
    # handshake).
    version_rows = (
        await db.execute(
            select(
                PeerHeartbeat.client_version.label("version"),
                func.count(PeerHeartbeat.peer_id).label("count"),
            ).group_by(PeerHeartbeat.client_version)
        )
    ).all()
    versions = {row.version: row.count for row in version_rows}

    release_count = await db.scalar(select(func.count(ContributorRelease.binary_hash)))
    last_release_at = await db.scalar(select(func.max(ContributorRelease.signed_at)))

    return {
        "as_of": now.isoformat(),
        "online_window_minutes": ONLINE_WINDOW_MINUTES,
        "peers": {
            "active_total": total_active or 0,
            "online_now": online or 0,
            "by_platform": platforms,
            "by_version": versions,
        },
        "releases": {
            "trusted_count": release_count or 0,
            "last_signed_at": last_release_at.isoformat() if last_release_at else None,
        },
    }
