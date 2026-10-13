"""Liveness backstop: reap peers whose heartbeat has gone stale.

`/peers/connect` never pings the peer - the coordinator only learns a peer
is gone via (a) a WebSocketDisconnect on the socket, or (b) a same-key
reconnect draining the old connection (see register_peer). A peer that
black-holes (process frozen, network partitioned one-way, etc.) without the
TCP connection actually tearing down produces neither: it just stops
heartbeating while still looking "connected" in the in-memory registry.

`pick_peer_for_model`'s HEARTBEAT_TTL_SECONDS read-side filter (in
peer_registry.py) stops new jobs from being routed to such a peer, but does
nothing about jobs already dispatched to it before it went stale, and does
nothing to free the registry slot. This module is the write-side half: a
periodic loop that finds peers whose last_beat_at is older than the same
TTL and actively drains their in-flight jobs (fast-fail the requester
instead of leaving it to hang out JOB_TIMEOUT_S) and unregisters them.

Only reaps peers that have a heartbeat row and it's stale - a peer that just
connected and hasn't sent its first heartbeat yet gets the same grace period
the read-side filter gives it (see HEARTBEAT_TTL_SECONDS in peer_registry.py).

Privacy / trust note: reaping is a liveness event, not a correctness
signal - `drain_peer_jobs` already marks the drained jobs
`error_category="peer_disconnected"`, a sentinel that (by construction,
see drain_peer_jobs' docstring) never feeds reputation or quality-score
writes. A peer that went stale because its laptop lid closed must not be
treated the same as one that returned a wrong answer.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

logger = structlog.get_logger(__name__)

# How often the reaper wakes and sweeps for stale peers.
REAP_INTERVAL_S = 30.0


async def reap_stale_peers(session_factory: async_sessionmaker) -> int:
    """One reap cycle. Returns the number of peers actually reaped.

    Snapshots the online peer_id -> session_id map first, then checks
    heartbeat staleness against that snapshot, then drains/unregisters each
    stale peer passing its snapshotted session_id - so a peer that
    reconnects (fresh session_id) between the snapshot and the reap action
    is left alone; `drain_peer_jobs`/`unregister_peer`'s session_id guard
    makes that a no-op rather than evicting the new connection (same
    reconnect-race hazard `register_peer`/`unregister_peer` document).
    """
    from coordinator.models.peer import PeerHeartbeat
    from coordinator.services import circle_links
    from coordinator.services.peer_registry import (
        HEARTBEAT_TTL_SECONDS,
        drain_peer_jobs,
        get_peer,
        online_peer_sessions,
        unregister_peer,
    )
    from sqlalchemy import select

    sessions = await online_peer_sessions()
    if not sessions:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_TTL_SECONDS)

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(PeerHeartbeat.peer_id).where(
                    PeerHeartbeat.peer_id.in_(list(sessions.keys())),
                    PeerHeartbeat.last_beat_at < cutoff,
                )
            )
        ).all()
    stale_ids = [row[0] for row in rows]

    reaped = 0
    for peer_id in stale_ids:
        session_id = sessions[peer_id]
        conn = await get_peer(peer_id)
        drained = await drain_peer_jobs(peer_id, session_id=session_id)
        removed = await unregister_peer(peer_id, session_id=session_id)
        if not removed:
            # Session moved on (reconnected) between the snapshot and here -
            # the guards above already no-op'd; nothing to log as a reap.
            continue
        reaped += 1
        # A reaped lender engine is a disconnect for its direct circle links.
        # Its own handler will not report it: the registry entry is already gone
        # when that handler finally unwinds. No-op when links are off.
        if conn is not None:
            circle_links.on_peer_gone(peer_id, conn.mesh_node_id)
        logger.info(
            "peer_reaped_stale_heartbeat",
            peer_id=str(peer_id),
            session_id=str(session_id),
            drained_job_ids=[str(j) for j in drained],
            ttl_s=HEARTBEAT_TTL_SECONDS,
        )
    return reaped


async def run_reaper_loop(stop: asyncio.Event, interval_s: float = REAP_INTERVAL_S) -> None:
    """Background loop wired into the coordinator lifespan (mirrors
    services/verification.py's run_verification_loop exactly: interruptible
    sleep, logged-and-swallowed per-iteration failures, clean stop).
    """
    from coordinator.db.database import get_session_factory

    logger.info("peer_reaper_loop_started", interval_s=interval_s)
    while not stop.is_set():
        try:
            factory = get_session_factory()
            reaped = await reap_stale_peers(factory)
            if reaped:
                logger.info("peer_reaper_cycle_reaped", n=reaped)
        except Exception:
            logger.exception("peer_reaper_loop_iteration_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
    logger.info("peer_reaper_loop_stopped")
