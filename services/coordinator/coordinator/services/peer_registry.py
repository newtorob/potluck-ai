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

from coordinator.schemas.peer import MAX_OUTPUT_TOKENS
from coordinator.services.credit_ledger import NEUTRAL_REPUTATION


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
    # The job's requested output-token budget (sampling.max_tokens). Billing is
    # capped to this so a peer that over-generates beyond the request can't
    # inflate credits. Defaults to the hard ceiling when the budget is unknown.
    max_tokens: int = MAX_OUTPUT_TOKENS
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


# A peer's reputation (0..1, neutral 0.5) hard-resets to 0.0 on a wrong-answer
# canary (an unambiguous fabrication signal) and decays toward 0 on sustained
# failures. Open-pool serving uses HYSTERESIS: a peer is dropped from the pool
# once it falls below NEUTRAL, and must climb all the way back to NEUTRAL before
# it can serve strangers again, not merely clear the 0.0 floor with one clean
# canary. The span from the fabrication floor (0.0) up to the re-entry bar
# (NEUTRAL 0.5) is the quarantine band: about five clean canaries to earn back.
# Never applied to My Machines, where you already trust your own hardware.
POOL_REPUTATION_FLOOR = 0.0  # where a confirmed fabrication lands
POOL_REENTRY_THRESHOLD = NEUTRAL_REPUTATION  # bar to (re)enter the open pool


async def pick_peer_for_model(
    db,  # AsyncSession; typed loosely to avoid sqlalchemy at module load
    model_slug: str,
    *,
    owner_member_id: uuid.UUID | None = None,
    enforce_pool_trust: bool = False,
) -> PeerConnection | None:
    """Pick a connected peer that advertises the requested model_slug.

    Joins the in-memory peer registry (only currently-connected WS
    sessions) against peer_capabilities (which the coordinator populated
    at attestation time). Most-recently-connected eligible peer wins -
    same rolling-upgrade rationale as pick_peer().

    When enforce_pool_trust is set (open-pool routing), peers whose reputation
    sits below POOL_REENTRY_THRESHOLD are dropped, so a peer that fell below
    neutral (a fabrication hard-reset, or sustained failures) cannot serve
    strangers until it has earned its way back to neutral via clean canaries
    (hysteresis: one clean pass off the 0.0 floor is not enough). A peer can
    still re-enter by rotating to a fresh device key; that Sybil churn is
    bounded by require_registered_keys + the canary/escrow loop, not by this
    gate. Returns None if no online peer serves this model.
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

    # Open-pool defense (hysteresis): drop peers whose reputation sits below the
    # re-entry bar (NEUTRAL). A wrong-answer canary hard-resets to 0.0, and the
    # peer stays out of the pool through the whole 0.0 -> NEUTRAL band until clean
    # canaries earn it back, so a single pass off the floor cannot put a proven
    # fabricator back in front of strangers. Honest-but-flaky peers (infra misses
    # nudge down a step) recover with one clean pass back to neutral; peers with
    # no heartbeat row yet are treated as neutral and kept. Never runs for My
    # Machines.
    if enforce_pool_trust:
        from coordinator.models.peer import PeerHeartbeat

        bad_rows = (await db.execute(
            select(PeerHeartbeat.peer_id).where(
                PeerHeartbeat.peer_id.in_(eligible_ids),
                PeerHeartbeat.reputation < POOL_REENTRY_THRESHOLD,
            )
        )).all()
        eligible_ids -= {row[0] for row in bad_rows}
        if not eligible_ids:
            return None

    # Re-acquire the lock to read the connection map (might have changed,
    # but the peer_id->PeerConnection mapping is stable for the lifetime
    # of any given session).
    async with _lock:
        # Walk in reverse (most-recent-first) and return the first eligible.
        # When owner_member_id is set ("My Machines" scope), restrict to peers
        # owned by that member. The connection already carries member_id, so
        # this is an in-memory filter with no extra DB round-trip.
        for peer_id in reversed(_peers.keys()):
            if peer_id not in eligible_ids:
                continue
            if owner_member_id is not None and _peers[peer_id].member_id != owner_member_id:
                continue
            return _peers[peer_id]
    return None


async def select_peer_for_request(
    db,
    *,
    model_slug: str,
    requester_member_id: uuid.UUID,
    scope: str = "pool",
) -> "tuple[PeerConnection | None, str | None]":
    """Pick a peer honoring the routing scope. `scope` is the WIDEST tier the
    requester accepts; we always try the closest/most-trusted tier first and
    widen outward only up to `scope`. Returns (peer, served_scope) or
    (None, None) if nothing in-scope is online.

      "mine" -> only machines this member owns.
      "pool" -> mine first, else the open pool.

    The "circle" tier is rejected at the API layer until a trusted-circle data
    model exists, so it never reaches here as a widening target.
    """
    # Tier 1 — My Machines (tried first for every scope).
    peer = await pick_peer_for_model(
        db, model_slug, owner_member_id=requester_member_id
    )
    if peer is not None:
        return peer, "mine"
    if scope == "mine":
        return None, None
    # Tier 3 — Open Pool. Gate on reputation: a peer that fell below neutral
    # (fabrication, or sustained failures) must earn its way back before it can
    # serve strangers again.
    peer = await pick_peer_for_model(db, model_slug, enforce_pool_trust=True)
    if peer is not None:
        return peer, "pool"
    return None, None


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


async def register_job(
    job_id: uuid.UUID, max_tokens: int = MAX_OUTPUT_TOKENS
) -> JobBuffer:
    """Create + register a JobBuffer the dispatch endpoint can await on.

    `max_tokens` is the job's requested output budget (sampling.max_tokens);
    billing is capped to it so peer over-generation can't inflate credits."""
    async with _lock:
        buf = JobBuffer(job_id=job_id, max_tokens=max_tokens)
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
