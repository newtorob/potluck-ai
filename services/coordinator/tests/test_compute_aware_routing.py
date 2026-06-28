"""Compute-and-load aware peer selection.

Verifies that pick_peer_for_model selects the peer most likely to answer
fastest — weighted by tokens_per_second (primary), hardware-tier proxy
(tps=null fallback), current_jobs (load), and reputation.

Registration order is intentional in each test: where the new scoring
behaviour differs from the old "most-recent-first" heuristic, the better
peer is registered FIRST (so it would LOSE the old recency race) to prove
the score is what drives the result.

All tests use db_session (single rolled-back transaction) and a unique
model slug so live DB peers cannot match.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.peer import Peer, PeerCapability, PeerHeartbeat
from coordinator.services.peer_registry import (
    PeerConnection,
    pick_peer_for_model,
    register_peer,
    unregister_peer,
)

# Unique slug: no live peer advertises this, so eligibility only comes from
# rows we insert in these tests.
_MODEL = "qwen3:4b-compute-routing-test"


class _WS:
    """Stub WebSocket — pick_peer_for_model stores but never calls it."""


async def _mk_peer(
    db: AsyncSession,
    member_id: uuid.UUID,
    *,
    tps: float | None = None,
    current_jobs: int = 0,
    reputation: float = 0.5,
    hardware_summary: dict | None = None,
    add_heartbeat: bool = True,
) -> uuid.UUID:
    """Insert Peer + PeerCapability (+ optional PeerHeartbeat) and register in-memory."""
    peer = Peer(
        member_id=member_id,
        public_key=uuid.uuid4().hex,
        hardware_summary=hardware_summary or {},
        status="active",
    )
    db.add(peer)
    await db.flush()
    db.add(PeerCapability(
        peer_id=peer.id,
        model_slug=_MODEL,
        quantization="q4_K_M",
        tokens_per_second=tps,
    ))
    if add_heartbeat:
        db.add(PeerHeartbeat(
            peer_id=peer.id,
            last_beat_at=datetime.now(timezone.utc),
            current_jobs=current_jobs,
            reputation=reputation,
            binary_hash="testhash",
            client_version="0.0.0",
        ))
    await db.flush()
    await register_peer(
        PeerConnection(peer_id=peer.id, session_id=uuid.uuid4(), ws=_WS(), member_id=member_id)
    )
    return peer.id


@pytest.mark.asyncio
async def test_higher_tps_wins(db_session: AsyncSession) -> None:
    """Peer with higher tokens_per_second is selected even when it connected
    earlier (would lose the old most-recent-first heuristic).

    Scores (compute / (1 + jobs) * (0.5 + rep)):
      fast: 50.0 / 1 * 1.0 = 50.0
      slow: 10.0 / 1 * 1.0 = 10.0  -- registered second (most recent)
    """
    member = uuid.uuid4()
    # fast registered first -> least recent -> old code would pick slow
    fast = await _mk_peer(db_session, member, tps=50.0, current_jobs=0, reputation=0.5)
    slow = await _mk_peer(db_session, member, tps=10.0, current_jobs=0, reputation=0.5)
    try:
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == fast
    finally:
        await unregister_peer(fast)
        await unregister_peer(slow)


@pytest.mark.asyncio
async def test_busier_peer_deprioritized(db_session: AsyncSession) -> None:
    """When tps is equal, the peer with more current_jobs loses.

    Scores:
      idle: 40.0 / 1 * 1.0 = 40.0  -- registered first (least recent)
      busy: 40.0 / 4 * 1.0 = 10.0  -- registered second (most recent)
    Old code would pick busy; new code picks idle.
    """
    member = uuid.uuid4()
    idle = await _mk_peer(db_session, member, tps=40.0, current_jobs=0, reputation=0.5)
    busy = await _mk_peer(db_session, member, tps=40.0, current_jobs=3, reputation=0.5)
    try:
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == idle
    finally:
        await unregister_peer(idle)
        await unregister_peer(busy)


@pytest.mark.asyncio
async def test_hardware_tier_fallback_gpu_beats_cpu(db_session: AsyncSession) -> None:
    """When tps is null, the hardware-tier proxy is used.

    hardware_summary currently carries {system, machine, cpu_count} from
    detect_hardware_summary().  Darwin+arm64 (Apple Silicon) maps to
    _HW_TIER_MEDIUM (2.0); Linux+x86_64 maps to _HW_TIER_CPU (1.0).

    Scores:
      gpu: 2.0 / 1 * 1.0 = 2.0  -- registered first (least recent)
      cpu: 1.0 / 1 * 1.0 = 1.0  -- registered second (most recent)
    Old code would pick cpu; new code picks gpu.
    """
    member = uuid.uuid4()
    gpu_peer = await _mk_peer(
        db_session, member,
        tps=None,
        hardware_summary={"system": "Darwin", "machine": "arm64", "cpu_count": 10},
    )
    cpu_peer = await _mk_peer(
        db_session, member,
        tps=None,
        hardware_summary={"system": "Linux", "machine": "x86_64", "cpu_count": 4},
    )
    try:
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == gpu_peer
    finally:
        await unregister_peer(gpu_peer)
        await unregister_peer(cpu_peer)


@pytest.mark.asyncio
async def test_hardware_tier_recommended_tier_field(db_session: AsyncSession) -> None:
    """When hardware_summary carries the extended recommended_tier field
    (future sidecar update), it takes precedence over system/machine.

    Scores:
      large_gpu: 4.0 / 1 * 1.0 = 4.0  -- registered first
      medium_gpu: 2.0 / 1 * 1.0 = 2.0 -- registered second (most recent)
    """
    member = uuid.uuid4()
    large_gpu = await _mk_peer(
        db_session, member,
        tps=None,
        hardware_summary={"system": "Linux", "machine": "x86_64", "recommended_tier": "large"},
    )
    medium_gpu = await _mk_peer(
        db_session, member,
        tps=None,
        hardware_summary={"system": "Linux", "machine": "x86_64", "recommended_tier": "medium"},
    )
    try:
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == large_gpu
    finally:
        await unregister_peer(large_gpu)
        await unregister_peer(medium_gpu)


@pytest.mark.asyncio
async def test_all_signals_equal_most_recent_wins(db_session: AsyncSession) -> None:
    """When all signals are identical, the most-recently-connected peer wins.
    This is the stable tiebreak and matches the pre-scoring behaviour exactly.

    Both peers:  score = 20.0 / 1 * 1.0 = 20.0
    peer_b registered second -> most recent -> wins.
    """
    member = uuid.uuid4()
    peer_a = await _mk_peer(db_session, member, tps=20.0, current_jobs=0, reputation=0.5)
    peer_b = await _mk_peer(db_session, member, tps=20.0, current_jobs=0, reputation=0.5)
    try:
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == peer_b
    finally:
        await unregister_peer(peer_a)
        await unregister_peer(peer_b)


@pytest.mark.asyncio
async def test_no_heartbeat_defaults_to_idle_neutral(db_session: AsyncSession) -> None:
    """A peer with no heartbeat row is treated as current_jobs=0, rep=NEUTRAL.
    It remains eligible and its score is computed from defaults.
    """
    member = uuid.uuid4()
    no_hb = await _mk_peer(db_session, member, tps=15.0, add_heartbeat=False)
    try:
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == no_hb
    finally:
        await unregister_peer(no_hb)


@pytest.mark.asyncio
async def test_tps_beats_hardware_tier(db_session: AsyncSession) -> None:
    """A peer with a measured tps=5 (slow CPU box) still beats a peer with
    tps=null and a high hardware tier, because measured data beats the proxy.

    Scores:
      measured: 5.0 / 1 * 1.0 = 5.0   -- registered first
      proxy: _HW_TIER_MEDIUM / 1 * 1.0 = 2.0  -- registered second (most recent)
    Old code picks proxy (most recent); new code picks measured.
    """
    member = uuid.uuid4()
    measured = await _mk_peer(db_session, member, tps=5.0, current_jobs=0, reputation=0.5)
    proxy = await _mk_peer(
        db_session, member,
        tps=None,
        hardware_summary={"system": "Darwin", "machine": "arm64", "cpu_count": 10},
    )
    try:
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == measured
    finally:
        await unregister_peer(measured)
        await unregister_peer(proxy)
