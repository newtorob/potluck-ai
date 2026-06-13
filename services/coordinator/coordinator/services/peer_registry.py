"""In-memory registry of live peer WebSocket connections + outstanding jobs.

This is the bridge between the long-lived /peers/connect WS handler (which
holds the actual socket to the peer) and the short-lived job dispatch
endpoint (which needs to send a JobAssignedMessage to a specific peer and
await its tokens).

Architecture:
- `_peers` maps peer_id → PeerConnection (the WS + session metadata).
- `_jobs` maps job_id → JobBuffer (in-flight token accumulator + done event).

Both are module-level singletons protected by an asyncio.Lock. This is
fine for v0 (single coordinator process); when we shard the coordinator
horizontally we'll need a Redis-backed equivalent (m4+ work).

Privacy: this module ONLY traffics in routing metadata (peer_id, job_id,
session_id) and the streaming token text in JobBuffer. Token text lives
in process memory for the duration of a job and is dropped after the
caller pulls the result. No persistence, no logging of token content.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket


@dataclass
class PeerConnection:
    peer_id: uuid.UUID
    session_id: uuid.UUID
    ws: WebSocket
    member_id: uuid.UUID  # used for OUR-side job creation only — never forwarded


@dataclass
class JobBuffer:
    """Accumulates streaming output for one job.

    Two consumer paths share the same buffer:
    - **Sync /jobs**: awaits `done` and concatenates `tokens` for the response.
    - **Streaming /jobs/stream**: reads `token_stream` (an asyncio.Queue) as
      tokens arrive and yields them as SSE chunks.

    The peer-side message handler in routers/peers.py pushes to BOTH on
    every JobTokenMessage so either consumer works without coordination.
    The queue is bounded (maxsize=512) so a runaway peer can't drive
    coordinator memory unbounded; if it fills up `put_nowait` raises
    QueueFull and the handler logs + drops, which the streaming consumer
    sees as a brief gap (acceptable; backpressure on the peer side comes
    in m4+).
    """

    job_id: uuid.UUID
    tokens: list[str] = field(default_factory=list)
    token_stream: asyncio.Queue[str] = field(
        default_factory=lambda: asyncio.Queue(maxsize=512)
    )
    output_tokens: int = 0
    duration_ms: int = 0
    finish_reason: str = ""
    error_category: str | None = None
    error_message: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


_peers: dict[uuid.UUID, PeerConnection] = {}
_jobs: dict[uuid.UUID, JobBuffer] = {}
_lock = asyncio.Lock()


# ---- peer connection lifecycle --------------------------------------- #


async def register_peer(conn: PeerConnection) -> None:
    """Add a peer's open WS to the registry. If the same peer_id already has
    a registered connection, replace it (the old one is presumed dead or
    being replaced by a reconnect)."""
    async with _lock:
        _peers[conn.peer_id] = conn


async def unregister_peer(peer_id: uuid.UUID) -> None:
    async with _lock:
        _peers.pop(peer_id, None)


async def pick_peer() -> PeerConnection | None:
    """Pick any connected peer regardless of capability. Used by stub-only
    code paths and for the legacy fallback when no model_slug is given.

    For real inference dispatch use `pick_peer_for_model(slug)` instead -
    it filters by what the peer actually advertises in peer_capabilities,
    which is what you want once contributors run different models.

    Most-recently-connected wins (helps during rolling upgrades; new code
    is more likely to handle new message types).
    """
    async with _lock:
        if not _peers:
            return None
        return next(reversed(_peers.values()))


async def pick_peer_for_model(
    db,  # AsyncSession; typed loosely to avoid sqlalchemy at module load
    model_slug: str,
) -> PeerConnection | None:
    """Pick a connected peer that advertises the requested model_slug.

    Joins the in-memory peer registry (only currently-connected WS
    sessions) against peer_capabilities (which the coordinator populated
    at attestation time). Most-recently-connected eligible peer wins -
    same rolling-upgrade rationale as pick_peer().

    Returns None if no online peer serves this model.
    """
    from sqlalchemy import select
    from coordinator.models import PeerCapability

    async with _lock:
        if not _peers:
            return None
        online_ids = list(_peers.keys())

    # Read peer_capabilities outside the asyncio lock - DB I/O can take
    # tens of ms and we don't want to serialize on it.
    rows = (await db.execute(
        select(PeerCapability.peer_id).where(
            PeerCapability.model_slug == model_slug,
            PeerCapability.peer_id.in_(online_ids),
        )
    )).all()
    eligible_ids = {row[0] for row in rows}

    if not eligible_ids:
        return None

    # Re-acquire the lock to read the connection map (might have changed,
    # but the peer_id->PeerConnection mapping is stable for the lifetime
    # of any given session).
    async with _lock:
        # Walk in reverse (most-recent-first) and return the first eligible.
        for peer_id in reversed(_peers.keys()):
            if peer_id in eligible_ids:
                return _peers[peer_id]
    return None


async def online_peer_count() -> int:
    async with _lock:
        return len(_peers)


async def get_peer(peer_id: uuid.UUID) -> PeerConnection | None:
    """Return a specific peer's live connection, or None if it isn't online.
    Used by coordinator-initiated probes (canary verification) that target a
    known peer rather than picking one by model."""
    async with _lock:
        return _peers.get(peer_id)


async def online_peer_ids() -> list[uuid.UUID]:
    """Snapshot of currently-connected peer ids. Used by the verification loop
    to intersect against peers that hold escrowed credits."""
    async with _lock:
        return list(_peers.keys())


# ---- job buffer lifecycle -------------------------------------------- #


async def register_job(job_id: uuid.UUID) -> JobBuffer:
    """Create + register a JobBuffer the dispatch endpoint can await on."""
    async with _lock:
        buf = JobBuffer(job_id=job_id)
        _jobs[job_id] = buf
        return buf


async def get_job(job_id: uuid.UUID) -> JobBuffer | None:
    async with _lock:
        return _jobs.get(job_id)


async def complete_job(job_id: uuid.UUID) -> None:
    """Signal the buffer's `done` event and drop it from the registry. The
    awaiting caller still holds a reference, so they can read tokens after
    the buffer is gone from the dict."""
    async with _lock:
        buf = _jobs.pop(job_id, None)
    if buf is not None:
        buf.done.set()
