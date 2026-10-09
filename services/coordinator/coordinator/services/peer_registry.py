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

      score = compute_score / (1 + current_jobs) * (0.5 + reputation) * quality

  compute_score = PeerCapability.tokens_per_second for this model_slug when
  available; otherwise a coarse hardware-tier proxy derived from
  Peer.hardware_summary (see _hardware_tier_score). current_jobs is
  max(live in-flight count, heartbeat-reported current_jobs) — the live
  count is process-authoritative and updated at dispatch time, so it can't
  go stale the way the heartbeat alone can (up to ~30s between beats); see
  `live_in_flight_counts`. reputation comes from the latest PeerHeartbeat
  row; missing heartbeat defaults to 0 jobs / neutral reputation.  quality
  comes from the sliding-window aggregate in services/peer_quality.py
  (default 1.0 for new peers). Stable tiebreak on equal scores:
  most-recently-connected peer wins (preserves pre-scoring behaviour when
  all signals are absent or equal).

  Liveness TTL exclude: before scoring, any peer whose latest heartbeat is
  older than HEARTBEAT_TTL_SECONDS (60s = 2x the 30s heartbeat interval) is
  dropped from eligible_ids outright — its heartbeat isn't just stale for
  load purposes, it's stale enough that the peer may be black-holed. A peer
  with NO heartbeat row yet (just connected) is exempt and stays eligible.

  Quality hard-exclude: before scoring, pick_peer_for_model removes any peer
  with >= QUALITY_CONSECUTIVE_FAIL_THRESHOLD (default 3) consecutive trailing
  failures from eligible_ids.  A single bad signal NEVER triggers exclusion.

  Capacity hard-exclude: a peer with Peer.max_concurrent_jobs set is dropped
  from eligible_ids once its LIVE in-flight count reaches that cap — a
  saturated peer is removed from candidacy entirely, not merely
  score-dampened. NULL (the default) means uncapped.

  RTT extension point: a latency factor can be multiplied into the score
  once a coordinator<->peer RTT measurement exists (see TODO in
  _score_peers). Geography is the small term per measurement (~+15 ms
  cross-country); do not invent a signal before it is measured.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import WebSocket

from coordinator.schemas.peer import MAX_OUTPUT_TOKENS
from coordinator.services.credit_ledger import NEUTRAL_REPUTATION

logger = structlog.get_logger(__name__)

# Heartbeat liveness TTL: 2x the peer's heartbeat_interval_s (30s, see
# schemas/peer.py AcceptMessage.heartbeat_interval_s). A peer that HAS a
# heartbeat row but hasn't beaten in this long is excluded from routing
# eligibility (pick_peer_for_model) and is fair game for the reaper
# (services/peer_reaper.py) to drain + unregister. A peer with NO heartbeat
# row yet (just connected, hasn't sent its first beat) is NOT covered by
# this — it gets a grace period and stays eligible, matching the existing
# "missing heartbeat -> neutral defaults" behaviour in _score_peers.
HEARTBEAT_TTL_SECONDS = 60


@dataclass
class PeerConnection:
    peer_id: uuid.UUID
    session_id: uuid.UUID
    ws: WebSocket
    member_id: uuid.UUID  # used for OUR-side job creation only — never forwarded
    # Who this peer agreed to serve beyond its owner's own requests (see
    # AttestationPayload.audiences). Both true for a peer that named none.
    serves_circle: bool = True
    serves_pool: bool = True
    # Direct circle links: the mesh node this engine's machine is and the port
    # its circle listener is bound to, both set at attestation. None for an
    # engine that predates links or whose binding was refused. A peer without
    # both is never offered a link; it still serves through the relay.
    mesh_node_id: uuid.UUID | None = None
    circle_port: int | None = None


AUDIENCES = ("circle", "pool")


def honoured_audiences(requested: list[str] | None) -> list[str] | None:
    """The audiences a peer asked for that this coordinator enforces, in a
    fixed order. None for a peer that named none (it serves both)."""
    if requested is None:
        return None
    return [a for a in AUDIENCES if a in requested]


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
    # Which peer this job was dispatched to, when known. Used only to key
    # `_peer_jobs` for drain-on-disconnect (see `drain_peer_jobs`); never
    # forwarded to any wire response. None for callers that don't thread a
    # peer_id through (keeps this additive/back-compat for any future caller).
    peer_id: uuid.UUID | None = None
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
# peer_id -> set of job_ids currently dispatched to that peer. Populated
# alongside `_jobs` in register_job(), discarded alongside `_jobs` in
# complete_job(). Lets a disconnect fail-fast every in-flight job for that
# peer (drain_peer_jobs) instead of leaving the requester's JobBuffer to time
# out at JOB_TIMEOUT_S with no signal that the contributor is gone.
_peer_jobs: dict[uuid.UUID, set[uuid.UUID]] = {}
_lock = asyncio.Lock()


# ---- peer connection lifecycle --------------------------------------- #


async def register_peer(conn: PeerConnection) -> None:
    """Add a peer's open WS to the registry. If the same peer_id already has
    a registered connection, replace it (the old one is presumed dead or
    being replaced by a reconnect).

    Reconnect-race guard: replacing the registry entry alone used to leave
    the OLD connection's in-flight jobs (`_peer_jobs`) pointing at a socket
    that can never deliver another JobTokenMessage/JobCompleteMessage —
    those jobs would hang until JOB_TIMEOUT_S with no failover signal. So on
    a same-key reconnect, drain the old connection's jobs FIRST (marking
    them `error_category="peer_disconnected"`), then swap in the new
    connection. See `drain_peer_jobs` and the session_id guard on
    `unregister_peer` for the other half of this race (the OLD socket's
    later `finally:` must not evict the connection we just registered here).
    """
    async with _lock:
        existing = _peers.get(conn.peer_id)
        _peers[conn.peer_id] = conn

    if existing is not None:
        drained = await drain_peer_jobs(conn.peer_id)
        if drained:
            logger.info(
                "peer_reconnect_drained_stale_jobs",
                peer_id=str(conn.peer_id),
                old_session_id=str(existing.session_id),
                new_session_id=str(conn.session_id),
                job_ids=[str(j) for j in drained],
            )


async def unregister_peer(
    peer_id: uuid.UUID, session_id: uuid.UUID | None = None
) -> bool:
    """Remove a peer's connection from the registry.

    If `session_id` is given, only unregister when the connection currently
    registered for `peer_id` is STILL this session. Without this guard, a
    same-key reconnect races the OLD connection's WS handler: `register_peer`
    already swapped in the NEW connection (and drained the old one's jobs —
    see above), but the OLD handler's `finally:` runs sometime after that
    with the OLD session_id and would otherwise evict the freshly-registered
    NEW connection. Returns True if a connection was actually removed.
    """
    async with _lock:
        current = _peers.get(peer_id)
        if current is None:
            return False
        if session_id is not None and current.session_id != session_id:
            return False
        _peers.pop(peer_id, None)
        return True


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


# A measured speed is still the peer's claim. Cap what it can buy in routing
# until claims are checked against observed job throughput.
TPS_SCORE_CAP = 200.0


def measured_compute_score(tps: float) -> float:
    return min(tps, TPS_SCORE_CAP)


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
    """Batch-fetch compute + load + quality signals and return a score per peer.

    Queries (3 total, each with an IN over the candidate set):
      1. PeerCapability: tokens_per_second for this model_slug.
      2. Peer LEFT JOIN PeerHeartbeat: hardware_summary + current_jobs + reputation.
      3. peer_quality_scores (sliding window): recency-weighted quality score.

    Score formula:
        score = compute_score / (1 + current_jobs) * (0.5 + reputation) * quality

    Missing heartbeat -> current_jobs=0, reputation=NEUTRAL_REPUTATION.
    tokens_per_second=NULL -> compute_score from _hardware_tier_score().
    No quality observations in window -> quality=1.0 (neutral; new peers not penalised).

    RTT extension point: multiply the final score by
        rtt_factor = 1 / (1 + rtt_ms / REF_RTT_MS)
    once a coordinator<->peer RTT measurement is available.  The geography
    term is small (~+15 ms cross-country per measurement) so it intentionally
    has no weight here yet.  Do NOT invent a latency signal.
    """
    from sqlalchemy import select
    from coordinator.models.peer import Peer, PeerCapability, PeerHeartbeat
    from coordinator.services.peer_quality import QUALITY_NEUTRAL, batch_quality_scores

    # Live in-flight counts (authoritative, updated at dispatch time — not the
    # up-to-30s-stale heartbeat current_jobs). One lock acquisition, no DB hit.
    live_counts = await live_in_flight_counts(peer_ids)

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

    # Query 3: quality scores (sliding-window recency-weighted mean).
    # Peers with no observations in the window default to QUALITY_NEUTRAL (1.0),
    # so brand-new peers are not penalised.
    quality_map: dict[uuid.UUID, float] = await batch_quality_scores(
        db, peer_ids, model_slug
    )

    scores: list[float] = []
    for pid in peer_ids:
        tps = tps_map.get(pid)
        hw_summary, raw_jobs, raw_rep = hw_hb_map.get(pid, ({}, None, None))

        # Compute signal: measured TPS when available; hardware tier proxy otherwise.
        if tps is not None and tps > 0.0:
            compute_score = measured_compute_score(tps)
        else:
            compute_score = _hardware_tier_score(hw_summary or {})

        # Load signal: the LIVE (process-authoritative, updated at dispatch)
        # in-flight count, floored against the heartbeat's self-reported
        # current_jobs so a peer's own report can only push the load term UP
        # (e.g. it's also serving something outside coordinator-dispatched
        # jobs), never mask a dispatch we just made that the peer hasn't
        # beaten about yet. This is what closes the "dispatch herd" gap: the
        # heartbeat alone can be up to ~30s stale, so two requests arriving
        # within that window used to both see current_jobs=0 and pile onto
        # the same idle-looking peer.
        heartbeat_jobs = raw_jobs if raw_jobs is not None else 0
        current_jobs = max(live_counts.get(pid, 0), heartbeat_jobs)

        # Reputation signal: default to neutral for brand-new peers.
        reputation = raw_rep if raw_rep is not None else NEUTRAL_REPUTATION

        # Quality signal: default to neutral (1.0) for peers with no history.
        quality = quality_map.get(pid, QUALITY_NEUTRAL)

        # TODO RTT: score *= 1 / (1 + rtt_ms / REF_RTT_MS)  when RTT is measured.
        score = compute_score / (1 + current_jobs) * (0.5 + reputation) * quality
        scores.append(score)

    return scores


async def pick_peer_for_model(
    db,  # AsyncSession; typed loosely to avoid sqlalchemy at module load
    model_slug: str,
    *,
    owner_member_id: uuid.UUID | None = None,
    enforce_pool_trust: bool = False,
    circle_member_pubkeys: set[str] | None = None,
    circle_member_ids: set[uuid.UUID] | None = None,
    exclude_peer_ids: set[uuid.UUID] | None = None,
    audience: str | None = None,
    peer_filter: Callable[[PeerConnection], bool] | None = None,
) -> PeerConnection | None:
    """Pick a connected peer that advertises the requested model_slug.

    `audience` ("circle" or "pool") names who the job is for when it is not
    the peer owner's own request; a peer that did not agree to serve that
    audience is never a candidate. None (My Machines) applies no such filter.

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

    When exclude_peer_ids is set (first-token failover, routers/jobs.py's
    `_dispatch_stream_with_failover`), those peer_ids are dropped from
    eligibility up front, alongside the TTL/quality/capacity excludes below —
    a peer that already proved dead-on-arrival for THIS request must never be
    re-picked on a retry attempt. Default None = current behavior (no
    exclusion), so every existing caller is unaffected.

    `peer_filter`, when set, is a plain (non-async) predicate on a peer's live
    connection; a peer it rejects is not a candidate. Direct circle links use
    it to offer only engines bound to a mesh node whose sidecar is connected
    and advertises links. It runs under the registry lock, so it must not
    wait. Default None applies no filter.
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

    if exclude_peer_ids:
        eligible_ids -= exclude_peer_ids

    if not eligible_ids:
        return None

    # Liveness TTL exclude: a peer whose latest heartbeat is older than
    # HEARTBEAT_TTL_SECONDS (60s = 2x the 30s heartbeat interval) is dropped
    # from eligibility outright, regardless of routing scope — dispatching to
    # it would otherwise just hang the requester out to JOB_TIMEOUT_S (120s)
    # with no signal, because a black-holed peer (no ping on /peers/connect)
    # never fires WebSocketDisconnect on its own. This is the READ-side half
    # of the liveness backstop; services/peer_reaper.py is the write-side
    # half (actively drains + unregisters the same stale peers on a timer).
    # A peer with NO heartbeat row yet (just connected, hasn't sent its first
    # beat) is exempt — same "missing heartbeat -> neutral defaults" grace
    # _score_peers already gives it.
    from coordinator.models.peer import PeerHeartbeat as _PeerHeartbeatTTL

    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_TTL_SECONDS)
    stale_rows = (await db.execute(
        select(_PeerHeartbeatTTL.peer_id).where(
            _PeerHeartbeatTTL.peer_id.in_(eligible_ids),
            _PeerHeartbeatTTL.last_beat_at < stale_cutoff,
        )
    )).all()
    if stale_rows:
        eligible_ids -= {row[0] for row in stale_rows}
        if not eligible_ids:
            return None

    # Quality hard-exclude: a peer with >= QUALITY_CONSECUTIVE_FAIL_THRESHOLD
    # consecutive trailing failures is removed from eligibility regardless of
    # routing scope (My Machines, circle, pool).  Requires N failures in a row
    # (default N=3) — a single bad signal NEVER triggers exclusion.  Batch
    # query over the candidate set so there is no per-peer round-trip.
    from coordinator.services.peer_quality import batch_consecutive_fail_excluded

    quality_excluded = await batch_consecutive_fail_excluded(
        db, list(eligible_ids), model_slug
    )
    if quality_excluded:
        eligible_ids -= quality_excluded
        if not eligible_ids:
            return None

    # Capacity hard-exclude: a peer with Peer.max_concurrent_jobs set is
    # removed from candidacy entirely once its LIVE in-flight count reaches
    # that cap — not merely score-dampened (a saturated peer that still has
    # the highest score would otherwise keep winning dispatch and queue up
    # behind itself). NULL (the default) means uncapped; nothing changes for
    # a peer that never sets it.
    from coordinator.models.peer import Peer as _PeerCapacity

    cap_rows = (await db.execute(
        select(_PeerCapacity.id, _PeerCapacity.max_concurrent_jobs).where(
            _PeerCapacity.id.in_(eligible_ids),
            _PeerCapacity.max_concurrent_jobs.isnot(None),
        )
    )).all()
    if cap_rows:
        capped_ids = [row[0] for row in cap_rows]
        live_counts_for_cap = await live_in_flight_counts(capped_ids)
        saturated = {
            pid
            for pid, cap in cap_rows
            if live_counts_for_cap.get(pid, 0) >= cap
        }
        if saturated:
            eligible_ids -= saturated
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
    if circle_member_pubkeys is not None or circle_member_ids is not None:
        from coordinator.models.peer import Peer
        from coordinator.models.member_key import MemberKey

        circle_member_ids = set(circle_member_ids or ())
        if circle_member_pubkeys:
            # Find member_ids that own at least one of the given pubkeys.
            mk_rows = (await db.execute(
                select(MemberKey.member_id).where(
                    MemberKey.public_key.in_(circle_member_pubkeys)
                )
            )).all()
            circle_member_ids |= {row[0] for row in mk_rows}

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
            if audience == "circle" and not _peers[peer_id].serves_circle:
                continue
            if audience == "pool" and not _peers[peer_id].serves_pool:
                continue
            if peer_filter is not None and not peer_filter(_peers[peer_id]):
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
    circle_member_ids: "set[uuid.UUID] | None" = None,
    exclude_peer_ids: "set[uuid.UUID] | None" = None,
    peer_filter: "Callable[[PeerConnection], bool] | None" = None,
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

    `exclude_peer_ids`, when set, is forwarded to every `pick_peer_for_model`
    call at every tier — a peer excluded because it was dead-on-arrival on a
    failover retry must stay excluded whether the retry re-checks My Machines,
    Circle, or Pool. Default None = current behavior.

    `peer_filter` is forwarded the same way (see pick_peer_for_model). Direct
    circle links call this function, with the circle's lender ids, so lender
    choice is the relay's own: the requester's machines stay excluded and each
    peer's sharing audience is honoured.
    """
    if scope == "circle":
        # Circle scope: dispatch strictly to peers in the circle. No fallback
        # to My Machines or Pool — the caller explicitly chose circle isolation.
        # The caller resolves the circle to the members who may lend
        # (circle_member_ids). circle_pubkeys is the older form, a set of
        # machine keys, resolved to their members here.
        if not circle_pubkeys and not circle_member_ids:
            return None, None
        peer = await pick_peer_for_model(
            db,
            model_slug,
            circle_member_pubkeys=circle_pubkeys or None,
            circle_member_ids=circle_member_ids or None,
            exclude_peer_ids=exclude_peer_ids,
            audience="circle",
            peer_filter=peer_filter,
        )
        if peer is not None:
            return peer, "circle"
        return None, None

    # Tier 1 — My Machines (tried first for mine and pool scopes).
    peer = await pick_peer_for_model(
        db,
        model_slug,
        owner_member_id=requester_member_id,
        exclude_peer_ids=exclude_peer_ids,
        peer_filter=peer_filter,
    )
    if peer is not None:
        return peer, "mine"
    if scope == "mine":
        return None, None
    # Tier 3 — Open Pool. Gate on reputation: a peer that fell below neutral
    # (fabrication, or sustained failures) must earn its way back before it can
    # serve strangers again.
    peer = await pick_peer_for_model(
        db, model_slug, enforce_pool_trust=True, exclude_peer_ids=exclude_peer_ids,
        audience="pool", peer_filter=peer_filter,
    )
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


async def circle_model_availability(db, member_ids: set[uuid.UUID]) -> dict[str, int]:
    """model_slug -> how many machines of these members are online and
    offering it to circles.

    Counts only: the machines are those of the given members (a circle's
    lenders for one requester) that are connected and agreed to serve
    circles, the same set a circle job can be dispatched to. Nothing here
    says whose machine it is.
    """
    from sqlalchemy import func, select
    from coordinator.models import PeerCapability

    if not member_ids:
        return {}
    async with _lock:
        peer_ids = [
            pid for pid, conn in _peers.items()
            if conn.member_id in member_ids and conn.serves_circle
        ]
    if not peer_ids:
        return {}
    rows = (await db.execute(
        select(PeerCapability.model_slug, func.count(func.distinct(PeerCapability.peer_id)))
        .where(PeerCapability.peer_id.in_(peer_ids))
        .group_by(PeerCapability.model_slug)
    )).all()
    return {slug: int(count) for slug, count in rows}


async def online_peer_sessions() -> dict[uuid.UUID, uuid.UUID]:
    """Snapshot of peer_id -> session_id for every currently-connected peer.

    Used by the reaper (services/peer_reaper.py) so it can pass `session_id`
    to `drain_peer_jobs`/`unregister_peer` — without it, a reap racing a
    same-key reconnect could evict the peer's brand-new connection instead
    of the stale one it actually meant to remove (same hazard the
    reconnect-race guard on `unregister_peer` documents).
    """
    async with _lock:
        return {pid: conn.session_id for pid, conn in _peers.items()}


# ---- live in-flight counter ------------------------------------------- #
#
# `_peer_jobs` (below) is already an atomic, `_lock`-protected map of
# peer_id -> set of job_ids currently dispatched to that peer: register_job()
# adds to it at THE moment of dispatch assignment, and complete_job() /
# drain_peer_jobs() remove from it on every terminal path (normal finish,
# peer-reported error, disconnect-drain, same-key-reconnect-drain). Its size
# per peer IS the live in-flight counter this module needs — reusing it
# (rather than introducing a second, separately-mutated counter) means there
# is no second lock, no ordering hazard, and no way for the two to drift out
# of sync, by construction. `live_in_flight_count`/`live_in_flight_counts`
# below just expose read access to that existing state.
#
# Note this also (correctly) counts canary probe jobs (services/verification.py
# also calls register_job/complete_job with peer_id set) — a canary occupies
# real capacity on the peer's connection exactly like a real job does, so it
# should count toward both the load-aware score term and the capacity cap.


async def live_in_flight_count(peer_id: uuid.UUID) -> int:
    """Current live in-flight job count for one peer (authoritative, not the
    up-to-30s-stale heartbeat current_jobs)."""
    async with _lock:
        return len(_peer_jobs.get(peer_id, ()))


async def live_in_flight_counts(
    peer_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Batch form of `live_in_flight_count` — one lock acquisition for the
    whole candidate set instead of one per peer."""
    async with _lock:
        return {pid: len(_peer_jobs.get(pid, ())) for pid in peer_ids}


# ---- job buffer lifecycle -------------------------------------------- #


async def register_job(
    job_id: uuid.UUID,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    peer_id: uuid.UUID | None = None,
) -> JobBuffer:
    """Create + register a JobBuffer the dispatch endpoint can await on.

    `max_tokens` is the job's requested output budget (sampling.max_tokens);
    billing is capped to it so peer over-generation can't inflate credits.

    `peer_id`, when given, is the contributor this job was dispatched to.
    It's threaded into `_peer_jobs` so `drain_peer_jobs` can fail this job
    fast if that peer disconnects mid-job, instead of the caller hanging
    until JOB_TIMEOUT_S. Callers that don't know the peer yet (there are
    none today, but keep this optional) simply don't get drain coverage."""
    async with _lock:
        buf = JobBuffer(job_id=job_id, max_tokens=max_tokens, peer_id=peer_id)
        _jobs[job_id] = buf
        if peer_id is not None:
            _peer_jobs.setdefault(peer_id, set()).add(job_id)
        return buf


async def get_job(job_id: uuid.UUID) -> JobBuffer | None:
    async with _lock:
        return _jobs.get(job_id)


async def complete_job(job_id: uuid.UUID) -> None:
    """Signal the buffer's `done` event and drop it from the registry. The
    awaiting caller still holds a reference, so they can read tokens after
    the buffer is gone from the dict. Also discards the job from
    `_peer_jobs` so a peer that later disconnects doesn't try to drain a
    job that already finished normally."""
    async with _lock:
        buf = _jobs.pop(job_id, None)
        if buf is not None and buf.peer_id is not None:
            peer_job_ids = _peer_jobs.get(buf.peer_id)
            if peer_job_ids is not None:
                peer_job_ids.discard(job_id)
                if not peer_job_ids:
                    _peer_jobs.pop(buf.peer_id, None)
    if buf is not None:
        buf.done.set()


async def drain_peer_jobs(
    peer_id: uuid.UUID, session_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """Fail every job currently dispatched to `peer_id` (called on
    disconnect, or on a same-key reconnect for the connection being
    replaced) so the requester gets a fast 502 instead of hanging out
    JOB_TIMEOUT_S (120s) with no signal that the contributor is gone.

    If `session_id` is given, only drains when the connection currently
    registered for `peer_id` is still this session — guards against a stale
    disconnect handler draining a freshly-reconnected session's brand-new
    jobs (see the reconnect-race note on `register_peer`/`unregister_peer`).

    NOTE — reputation/quality: "peer_disconnected" is a COORDINATOR-
    SYNTHESIZED sentinel error_category, not a peer-reported one. It must
    NEVER feed a reputation or quality-score slash: a dropped connection is
    not evidence the peer fabricated or mishandled the job (could be a
    laptop lid closing, wifi flap, etc). Today neither the disconnect path
    nor `_finalize_job_in_db`'s error branch writes any reputation/quality
    row for a failed job, so this is safe by construction — but if a future
    "penalize peers for job failures" signal is added, it MUST filter this
    sentinel out explicitly rather than assume every error_category is a
    peer-caused fault.
    """
    async with _lock:
        if session_id is not None:
            current = _peers.get(peer_id)
            if current is None or current.session_id != session_id:
                return []
        job_ids = _peer_jobs.pop(peer_id, set())
        buffers: list[JobBuffer] = []
        for job_id in job_ids:
            buf = _jobs.pop(job_id, None)
            if buf is not None:
                buffers.append(buf)

    # Set the error state + fire `done` OUTSIDE the lock: `done.set()` wakes
    # up whoever is awaiting `buf.done.wait()` in routers/jobs.py, and we
    # don't want that wakeup (and its own downstream work) serialized behind
    # our registry lock.
    for buf in buffers:
        buf.error_category = "peer_disconnected"
        buf.error_message = "contributor peer disconnected mid-job"
        buf.done.set()

    return [buf.job_id for buf in buffers]
