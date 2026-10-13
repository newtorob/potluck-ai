"""Public-facing fleet aggregates. No member_id, no peer_id, no
individual peer details - only counts.

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
from coordinator.models import ContributorRelease, Job, Peer, PeerCapability, PeerHeartbeat

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

    # Models the network can currently serve. JOIN peer_capabilities with
    # online peers and group by slug - gives the desktop /network view a
    # "Available models" chip rack with a count per slug. No peer_id, no
    # hostname; just slug + count, fully aggregate.
    online_subq = (
        select(Peer.id)
        .where(Peer.status == "active", Peer.last_seen_at > online_threshold)
        .subquery()
    )
    model_rows = (
        await db.execute(
            select(
                PeerCapability.model_slug.label("slug"),
                func.count(func.distinct(PeerCapability.peer_id)).label("count"),
            )
            .where(PeerCapability.peer_id.in_(select(online_subq.c.id)))
            .group_by(PeerCapability.model_slug)
            .order_by(func.count(func.distinct(PeerCapability.peer_id)).desc())
        )
    ).all()
    models = [{"slug": row.slug, "online_peer_count": row.count} for row in model_rows]

    # Activity totals over the last 24h. Counts completed jobs and sums
    # output_tokens. No member_id grouping here - this is the public
    # fleet-wide view. Per-member stats live at /jobs/stats.
    day_ago = now - timedelta(hours=24)
    jobs_24h = await db.scalar(
        select(func.count(Job.id)).where(
            Job.completed_at > day_ago, Job.status == "completed"
        )
    )
    tokens_24h = await db.scalar(
        select(func.coalesce(func.sum(Job.output_tokens), 0)).where(
            Job.completed_at > day_ago, Job.status == "completed"
        )
    )

    return {
        "as_of": now.isoformat(),
        "online_window_minutes": ONLINE_WINDOW_MINUTES,
        "peers": {
            "active_total": total_active or 0,
            "online_now": online or 0,
            "by_platform": platforms,
            "by_version": versions,
        },
        "models": models,
        "activity": {
            "jobs_completed_24h": int(jobs_24h or 0),
            "tokens_served_24h": int(tokens_24h or 0),
        },
        "releases": {
            "trusted_count": release_count or 0,
            "last_signed_at": last_release_at.isoformat() if last_release_at else None,
        },
    }
