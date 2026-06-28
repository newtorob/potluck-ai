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

Peer selection (pick_peer_for_model):
  Eligible peers (model match + trust gates) are ranked by a composite score:

      score = compute_score / (1 + current_jobs) * (0.5 + reputation)

  compute_score = PeerCapability.tokens_per_second for this model_slug when
  available; otherwise a coarse hardware-tier proxy derived from
  Peer.hardware_summary (see _hardware_tier_score). current_jobs and
  reputation come from the latest PeerHeartbeat row; missing heartbeat
  defaults to 0 jobs / neutral reputation. Stable tiebreak on equal scores:
  most-recently-connected peer wins (preserves pre-scoring behaviour when all
  signals are absent or equal).

  RTT extension point: a latency factor can be multiplied into the score
  once a coordinator<->peer RTT measurement exists (see TODO in
  _score_peers). Geography is the small term per measurement (~+15 ms
  cross-country); do not invent a signal before it is measured.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

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

# ---------------------------------------------------------------------------
# Compute-tier proxy (used when PeerCapability.tokens_per_second is NULL)
# ---------------------------------------------------------------------------
#
# Tier constants (arbitrary scale; only relative ordering matters).
# These map the coarse hardware bucket to an approximate compute score so the
# same formula (score = compute / (1 + jobs) * (0.5 + rep)) works whether TPS
# is measured or estimated.
#
# Hierarchy: _HW_TIER_HIGH >> _HW_TIER_MEDIUM > _HW_TIER_LOW > _HW_TIER_CPU
_HW_TIER_HIGH   = 4.0   # Large dedicated GPU (VRAM >= 24 GB); future path
_HW_TIER_MEDIUM = 2.0   # Mid-tier GPU (VRAM >= 8 GB) OR Apple Silicon (current proxy)
_HW_TIER_LOW    = 1.5   # Small dedicated GPU (VRAM < 8 GB); future path
_HW_TIER_CPU    = 1.0   # CPU-only or unrecognised hardware


def _hardware_tier_score(hw: dict[str, Any]) -> float:
    """Derive a coarse compute-tier score from a Peer.hardware_summary dict.

    Primary path (extended hardware_summary — not yet sent by the coordinator
    attestation path, but will be once the sidecar upgrades detect_hardware_summary
    to forward full detect_hardware() output):
      recommended_tier: "large" | "medium" | "small"
      gpu_name        : str | None      -- any truthy value = some GPU
      gpu_vram_gb     : float | None    -- VRAM in gigabytes

    Current fallback path (fields actually present in the coordinator DB today,
    from contributor/client.py detect_hardware_summary()):
      system  : "Darwin" | "Linux" | "Windows"
      machine : "arm64" | "x86_64" | "AMD64" | ...

    TODO: remove the fallback paths when the attestation path sends full
    hardware_detect() output and the coordinator DB backfills existing rows.
    """
    # --- Extended fields (future; use when present) ---
    recommended_tier = hw.get("recommended_tier")
    if recommended_tier == "large":
        return _HW_TIER_HIGH
    if recommended_tier == "medium":
        return _HW_TIER_MEDIUM
    if recommended_tier == "small":
        return _HW_TIER_CPU

    gpu_name = hw.get("gpu_name")
    if gpu_name:
        vram = float(hw.get("gpu_vram_gb") or 0.0)
        if vram >= 24.0:
            return _HW_TIER_HIGH
        if vram >= 8.0:
            return _HW_TIER_MEDIUM
        return _HW_TIER_LOW

    # --- Current fallback: system + machine only ---
    # Apple Silicon is the one architecture where the coordinator reliably
    # knows there is strong GPU-class inference hardware (Metal unified memory)
    # without a dedicated GPU record.
    system  = hw.get("system", "")
    machine = hw.get("machine", "")
    if system == "Darwin" and machine == "arm64":
        return _HW_TIER_MEDIUM

    return _HW_TIER_CPU


async def _score_peers(
    db,  # AsyncSession — typed loosely to avoid sqlalchemy at module load
    peer_ids: list[uuid.UUID],
    model_slug: str,
) -> list[float]:
    """Batch-fetch compute + load signals and return a score per peer.

    Queries (2 total, each with an IN over the candidate set):
      1. PeerCapability: tokens_per_second for this model_slug.
      2. Peer LEFT JOIN PeerHeartbeat: hardware_summary + current_jobs + reputation.

    Score formula:
        score = compute_score / (1 + current_jobs) * (0.5 + reputation)

    Missing heartbeat -> current_jobs=0, reputation=NEUTRAL_REPUTATION.
    tokens_per_second=NULL -> compute_score from _hardware_tier_score().

    RTT extension point: multiply the final score by
        rtt_factor = 1 / (1 + rtt_ms / REF_RTT_MS)
    once a coordinator<->peer RTT measurement is available.  The geography
    term is small (~+15 ms cross-country per measurement) so it intentionally
    has no weight here yet.  Do NOT invent a latency signal.
    """
    from sqlalchemy import select
    from coordinator.models.peer import Peer, PeerCapability, PeerHeartbeat

    # Query 1: tokens_per_second per peer for this model slug.
    tps_rows = (await db.execute(
        select(PeerCapability.peer_id, PeerCapability.tokens_per_second).where(
            PeerCapability.peer_id.in_(peer_ids),
            PeerCapability.model_slug == model_slug,
        )
    )).all()
    tps_map: dict[uuid.UUID, float | None] = {row[0]: row[1] for row in tps_rows}

    # Query 2: hardware_summary + latest heartbeat (LEFT JOIN; NULL when no heartbeat).
    hw_hb_rows = (await db.execute(
        select(
            Peer.id,
            Peer.hardware_summary,
            PeerHeartbeat.current_jobs,
            PeerHeartbeat.reputation,
        )
        .join(PeerHeartbeat, PeerHeartbeat.peer_id == Peer.id, isouter=True)
        .where(Peer.id.in_(peer_ids))
    )).all()
    hw_hb_map: dict[uuid.UUID, tuple[dict, int | None, float | None]] = {
        row[0]: (row[1], row[2], row[3]) for row in hw_hb_rows
    }

    scores: list[float] = []
    for pid in peer_ids:
        tps = tps_map.get(pid)
        hw_summary, raw_jobs, raw_rep = hw_hb_map.get(pid, ({}, None, None))

        # Compute signal: measured TPS when available; hardware tier proxy otherwise.
        if tps is not None and tps > 0.0:
            compute_score = tps
        else:
            compute_score = _hardware_tier_score(hw_summary or {})

        # Load signal: default to idle when no heartbeat row exists yet.
        current_jobs = raw_jobs if raw_jobs is not None else 0

        # Reputation signal: default to neutral for brand-new peers.
        reputation = raw_rep if raw_rep is not None else NEUTRAL_REPUTATION

        # TODO RTT: score *= 1 / (1 + rtt_ms / REF_RTT_MS)  when RTT is measured.
        score = compute_score / (1 + current_jobs) * (0.5 + reputation)
        scores.append(score)

    return scores


async def pick_peer_for_model(
    db,  # AsyncSession; typed loosely to avoid sqlalchemy at module load
    model_slug: str,
    *,
    owner_member_id: uuid.UUID | None = None,
    enforce_pool_trust: bool = False,
    circle_member_pubkeys: set[str] | None = None,
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

    When circle_member_pubkeys is set (trusted-circle routing), only peers
    whose member's public_key is in that set are eligible. The filter is
    applied via a DB join against member_keys (one row per enrolled device
    key, keyed by public_key -> member_id). No extra trust gating is applied
    for circle peers — the owner chose them; trust is the owner's to grant.
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

    # Trusted-circle filter: restrict to peers whose member's key is in the circle.
    # Joins peer_id -> peers.member_id -> member_keys.public_key. Uses the Peer
    # model (not the in-memory registry) to get the canonical member_id per peer.
    if circle_member_pubkeys is not None:
        from coordinator.models.peer import Peer
        from coordinator.models.member_key import MemberKey

        # Find member_ids that own at least one of the circle's pubkeys.
        mk_rows = (await db.execute(
            select(MemberKey.member_id).where(
                MemberKey.public_key.in_(circle_member_pubkeys)
            )
        )).all()
        circle_member_ids: set[uuid.UUID] = {row[0] for row in mk_rows}

        if not circle_member_ids:
            # No enrolled members match any of the pubkeys — no eligible peer.
            return None

        # Keep only peers whose member_id is in the circle.
        peer_rows = (await db.execute(
            select(Peer.id).where(
                Peer.id.in_(eligible_ids),
                Peer.member_id.in_(circle_member_ids),
            )
        )).all()
        eligible_ids = {row[0] for row in peer_rows}
        if not eligible_ids:
            return None

    # Collect candidates in most-recent-first order (the stable tiebreak when
    # scores are equal — preserves pre-scoring behaviour for identical peers).
    # Apply the owner_member_id filter here; it is an in-memory check against
    # PeerConnection.member_id with no extra DB round-trip.
    async with _lock:
        if not _peers:
            return None
        candidates: list[uuid.UUID] = []
        for peer_id in reversed(_peers.keys()):
            if peer_id not in eligible_ids:
                continue
            if owner_member_id is not None and _peers[peer_id].member_id != owner_member_id:
                continue
            candidates.append(peer_id)
        if not candidates:
            return None
        if len(candidates) == 1:
            return _peers[candidates[0]]
        # Snapshot the connection map so we can return the winner without
        # holding the lock across the DB scoring queries.
        peer_map = {pid: _peers[pid] for pid in candidates}

    # Score all candidates with batched DB queries (2 round-trips regardless
    # of candidate count).  Pick the highest score; tiebreak = smallest index
    # in `candidates` (= most-recently-connected, matching the old behaviour).
    scores = await _score_peers(db, candidates, model_slug)
    best_idx = max(range(len(candidates)), key=lambda i: (scores[i], -i))
    return peer_map[candidates[best_idx]]


async def select_peer_for_request(
    db,
    *,
    model_slug: str,
    requester_member_id: uuid.UUID,
    scope: str = "pool",
    circle_pubkeys: "set[str] | None" = None,
) -> "tuple[PeerConnection | None, str | None]":
    """Pick a peer honoring the routing scope. `scope` is the WIDEST tier the
    requester accepts; we always try the closest/most-trusted tier first and
    widen outward only up to `scope`. Returns (peer, served_scope) or
    (None, None) if nothing in-scope is online.

      "mine"   -> only machines this member owns.
      "circle" -> only machines whose member's pubkey is in circle_pubkeys.
      "pool"   -> mine first, else the open pool.

    When scope="circle", circle_pubkeys must be provided (the set of member
    pubkeys from the chosen TrustedCircle). The caller (routers/jobs.py) has
    already validated that the circle belongs to the requester.
    """
    if scope == "circle":
        # Circle scope: dispatch strictly to peers in the circle. No fallback
        # to My Machines or Pool — the caller explicitly chose circle isolation.
        peer = await pick_peer_for_model(
            db, model_slug, circle_member_pubkeys=circle_pubkeys
        )
        if peer is not None:
            return peer, "circle"
        return None, None

    # Tier 1 — My Machines (tried first for mine and pool scopes).
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
