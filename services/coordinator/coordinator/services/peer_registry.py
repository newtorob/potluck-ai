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
    """Accumulates streaming output for one job. The dispatch endpoint
    awaits `done` and reads `tokens` + `error`."""

    job_id: uuid.UUID
    tokens: list[str] = field(default_factory=list)
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
    """Pick any connected peer for stub-mode dispatch. Capability-aware
    selection lands in step 6 of the m3 plan.

    For now we pick the MOST RECENTLY connected peer. Rationale: during
    rolling contributor upgrades, newly-connected peers are running the
    newest code and are most likely to handle new message types correctly.
    Older long-lived connections might be running stale code that
    silently drops job_assigned. This is a stop-gap until capability
    registration ships and we can filter by supported message versions.
    """
    async with _lock:
        if not _peers:
            return None
        return next(reversed(_peers.values()))


async def online_peer_count() -> int:
    async with _lock:
        return len(_peers)


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
