"""Pool-reliability spine: live in-flight counter, heartbeat-TTL liveness
(read-side filter + reaper), and per-peer capacity hard-exclude.

Scope: load/liveness/capacity only. Does NOT touch active failover/re-dispatch
(follow-on) or the open-pool trust items (canary/k_min/escrow — covered by
test_verification.py / test_credit_escrow.py).

Conventions match the sibling routing/drain test files:
  - test_compute_aware_routing.py's `_mk_peer` helper + registration-order
    trick (the peer that SHOULD win is registered FIRST, so it would LOSE
    the old most-recent-first tiebreak, proving the new signal is what
    actually decided the outcome).
  - test_job_drain_on_disconnect.py's direct peer_registry manipulation
    (module-global in-memory state; register/unregister in try/finally).
  - conftest.py's `session_factory` fixture for tests that call functions
    which open their own sessions internally (here: reap_stale_peers).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.peer import Peer, PeerCapability, PeerHeartbeat
from coordinator.services.peer_registry import (
    HEARTBEAT_TTL_SECONDS,
    PeerConnection,
    complete_job,
    drain_peer_jobs,
    get_peer,
    live_in_flight_count,
    live_in_flight_counts,
    pick_peer_for_model,
    register_job,
    register_peer,
    unregister_peer,
)

# Unique slug per module so live/dev-DB peers can never match these tests.
_MODEL = "qwen3:4b-pool-reliability-test"


class _WS:
    """Stub WebSocket — never actually sent to in these tests."""


async def _mk_peer(
    db: AsyncSession,
    member_id: uuid.UUID,
    *,
    tps: float | None = None,
    current_jobs: int = 0,
    reputation: float = 0.5,
    max_concurrent_jobs: int | None = None,
    last_beat_at: datetime | None = None,
    add_heartbeat: bool = True,
) -> uuid.UUID:
    """Insert Peer + PeerCapability (+ optional PeerHeartbeat) and register
    it in the in-memory connection registry. Mirrors
    test_compute_aware_routing.py's helper, plus the two new knobs this
    file needs (max_concurrent_jobs, last_beat_at)."""
    peer = Peer(
        member_id=member_id,
        public_key=uuid.uuid4().hex,
        hardware_summary={},
        status="active",
        max_concurrent_jobs=max_concurrent_jobs,
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
            last_beat_at=last_beat_at or datetime.now(timezone.utc),
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


# ---------------------------------------------------------------------------
# 1. Live in-flight counter — basic correctness + all terminal paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_in_flight_count_increments_on_register_job() -> None:
    peer_id = uuid.uuid4()
    await register_peer(
        PeerConnection(peer_id=peer_id, session_id=uuid.uuid4(), ws=_WS(), member_id=uuid.uuid4())
    )
    try:
        assert await live_in_flight_count(peer_id) == 0
        job_a = uuid.uuid4()
        job_b = uuid.uuid4()
        await register_job(job_a, peer_id=peer_id)
        assert await live_in_flight_count(peer_id) == 1
        await register_job(job_b, peer_id=peer_id)
        assert await live_in_flight_count(peer_id) == 2
    finally:
        await complete_job(job_a)
        await complete_job(job_b)
        await unregister_peer(peer_id)


@pytest.mark.asyncio
async def test_live_in_flight_count_decrements_on_complete_job() -> None:
    """Normal-finalize terminal path."""
    peer_id = uuid.uuid4()
    await register_peer(
        PeerConnection(peer_id=peer_id, session_id=uuid.uuid4(), ws=_WS(), member_id=uuid.uuid4())
    )
    try:
        job_id = uuid.uuid4()
        await register_job(job_id, peer_id=peer_id)
        assert await live_in_flight_count(peer_id) == 1
        await complete_job(job_id)
        assert await live_in_flight_count(peer_id) == 0
    finally:
        await unregister_peer(peer_id)


@pytest.mark.asyncio
async def test_live_in_flight_count_decrements_on_drain() -> None:
    """Disconnect/drain terminal path."""
    peer_id = uuid.uuid4()
    await register_peer(
        PeerConnection(peer_id=peer_id, session_id=uuid.uuid4(), ws=_WS(), member_id=uuid.uuid4())
    )
    try:
        job_a = uuid.uuid4()
        job_b = uuid.uuid4()
        await register_job(job_a, peer_id=peer_id)
        await register_job(job_b, peer_id=peer_id)
        assert await live_in_flight_count(peer_id) == 2
        drained = await drain_peer_jobs(peer_id)
        assert set(drained) == {job_a, job_b}
        assert await live_in_flight_count(peer_id) == 0
    finally:
        await unregister_peer(peer_id)


@pytest.mark.asyncio
async def test_live_in_flight_counts_batch_form() -> None:
    peer_a, peer_b = uuid.uuid4(), uuid.uuid4()
    await register_peer(
        PeerConnection(peer_id=peer_a, session_id=uuid.uuid4(), ws=_WS(), member_id=uuid.uuid4())
    )
    await register_peer(
        PeerConnection(peer_id=peer_b, session_id=uuid.uuid4(), ws=_WS(), member_id=uuid.uuid4())
    )
    try:
        job_a = uuid.uuid4()
        await register_job(job_a, peer_id=peer_a)
        counts = await live_in_flight_counts([peer_a, peer_b])
        assert counts == {peer_a: 1, peer_b: 0}
    finally:
        await complete_job(job_a)
        await unregister_peer(peer_a)
        await unregister_peer(peer_b)


# ---------------------------------------------------------------------------
# 2. Herd fix — the live counter (not the up-to-30s-stale heartbeat) drives
#    scoring, so two near-simultaneous dispatches don't both pile onto the
#    same peer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_counter_deprioritizes_peer_with_stale_zero_heartbeat(
    db_session: AsyncSession,
) -> None:
    """Both peers report current_jobs=0 in their (not-yet-refreshed)
    heartbeat row -- the exact staleness window this fix closes. One of
    them has a job registered in-flight via register_job (as
    _dispatch_to_peer does at dispatch time) with NO corresponding
    heartbeat update (heartbeats land up to 30s later). Without consulting
    the live counter, both would tie at current_jobs=0 and the
    most-recently-connected peer would win the tiebreak -- so `busy` is
    registered SECOND (most recent) to prove the live counter, not
    recency, is what deprioritizes it.
    """
    member = uuid.uuid4()
    idle = await _mk_peer(db_session, member, tps=40.0, current_jobs=0, reputation=0.5)
    busy = await _mk_peer(db_session, member, tps=40.0, current_jobs=0, reputation=0.5)

    job_id = uuid.uuid4()
    await register_job(job_id, peer_id=busy)
    try:
        assert await live_in_flight_count(busy) == 1
        assert await live_in_flight_count(idle) == 0

        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == idle, (
            "pick_peer_for_model picked the peer with a live in-flight job "
            "over the truly idle one -- the live counter was not consulted "
            "(scoring fell back to the stale heartbeat current_jobs=0 tie "
            "+ most-recent-wins tiebreak)."
        )
    finally:
        await complete_job(job_id)
        await unregister_peer(idle)
        await unregister_peer(busy)


@pytest.mark.asyncio
async def test_two_concurrent_dispatches_split_across_peers(
    db_session: AsyncSession,
) -> None:
    """Simulates the herd scenario end to end: pick once (gets peer A),
    register the job (as dispatch does), pick again immediately (before any
    heartbeat could possibly have landed) -- must get peer B, not pile a
    second job onto A."""
    member = uuid.uuid4()
    peer_a = await _mk_peer(db_session, member, tps=30.0, current_jobs=0, reputation=0.5)
    peer_b = await _mk_peer(db_session, member, tps=30.0, current_jobs=0, reputation=0.5)

    job_ids: list[uuid.UUID] = []
    try:
        first = await pick_peer_for_model(db_session, _MODEL)
        assert first is not None
        job1 = uuid.uuid4()
        await register_job(job1, peer_id=first.peer_id)
        job_ids.append(job1)

        second = await pick_peer_for_model(db_session, _MODEL)
        assert second is not None
        assert second.peer_id != first.peer_id, (
            "second dispatch piled onto the same peer as the first -- "
            "the dispatch herd this fix exists to close."
        )
        assert {first.peer_id, second.peer_id} == {peer_a, peer_b}
    finally:
        for j in job_ids:
            await complete_job(j)
        await unregister_peer(peer_a)
        await unregister_peer(peer_b)


# ---------------------------------------------------------------------------
# 3. Heartbeat-TTL read-side filter.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_heartbeat_peer_excluded_from_eligibility(
    db_session: AsyncSession,
) -> None:
    member = uuid.uuid4()
    stale_cutoff_breach = datetime.now(timezone.utc) - timedelta(
        seconds=HEARTBEAT_TTL_SECONDS + 30
    )
    stale = await _mk_peer(
        db_session, member, tps=10.0, last_beat_at=stale_cutoff_breach
    )
    fresh = await _mk_peer(db_session, member, tps=1.0)  # much lower score, still wins
    try:
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == fresh, (
            "a peer whose heartbeat is older than HEARTBEAT_TTL_SECONDS "
            "must be excluded from eligibility even though it would "
            "otherwise win on score."
        )
    finally:
        await unregister_peer(stale)
        await unregister_peer(fresh)


@pytest.mark.asyncio
async def test_no_heartbeat_peer_kept_eligible(db_session: AsyncSession) -> None:
    """A peer with NO heartbeat row yet (just connected) is exempt from the
    TTL exclude — same grace period _score_peers already gives it."""
    member = uuid.uuid4()
    no_hb = await _mk_peer(db_session, member, tps=10.0, add_heartbeat=False)
    try:
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == no_hb
    finally:
        await unregister_peer(no_hb)


@pytest.mark.asyncio
async def test_fresh_heartbeat_peer_stays_eligible(db_session: AsyncSession) -> None:
    """Sanity: a peer well within the TTL is unaffected."""
    member = uuid.uuid4()
    fresh = await _mk_peer(
        db_session,
        member,
        tps=10.0,
        last_beat_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    try:
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == fresh
    finally:
        await unregister_peer(fresh)


# ---------------------------------------------------------------------------
# 4. Reaper — the write-side half of the liveness backstop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_drains_and_unregisters_stale_peer(session_factory) -> None:
    from coordinator.services.peer_reaper import reap_stale_peers

    peer_id = uuid.uuid4()
    async with session_factory.factory() as db:
        peer = Peer(member_id=uuid.uuid4(), public_key=uuid.uuid4().hex, status="active")
        db.add(peer)
        await db.flush()
        peer_id = peer.id
        db.add(PeerHeartbeat(
            peer_id=peer_id,
            last_beat_at=datetime.now(timezone.utc)
            - timedelta(seconds=HEARTBEAT_TTL_SECONDS + 15),
            current_jobs=0,
            reputation=0.5,
            binary_hash="testhash",
            client_version="0.0.0",
        ))
        await db.commit()
    session_factory.track_peer(peer_id)

    conn = PeerConnection(
        peer_id=peer_id, session_id=uuid.uuid4(), ws=_WS(), member_id=uuid.uuid4()
    )
    await register_peer(conn)
    job_id = uuid.uuid4()
    buf = await register_job(job_id, peer_id=peer_id)
    try:
        assert await get_peer(peer_id) is conn
        assert not buf.done.is_set()

        reaped = await reap_stale_peers(session_factory.factory)
        assert reaped == 1

        # In-flight job was drained fast (not left to hang JOB_TIMEOUT_S)...
        assert buf.done.is_set()
        assert buf.error_category == "peer_disconnected"
        # ...and the connection is gone from the registry.
        assert await get_peer(peer_id) is None
        assert await live_in_flight_count(peer_id) == 0
    finally:
        # Best-effort: reap already unregistered/drained on success.
        await drain_peer_jobs(peer_id)
        await unregister_peer(peer_id)


@pytest.mark.asyncio
async def test_reaper_leaves_fresh_heartbeat_peer_alone(session_factory) -> None:
    from coordinator.services.peer_reaper import reap_stale_peers

    peer_id = uuid.uuid4()
    async with session_factory.factory() as db:
        peer = Peer(member_id=uuid.uuid4(), public_key=uuid.uuid4().hex, status="active")
        db.add(peer)
        await db.flush()
        peer_id = peer.id
        db.add(PeerHeartbeat(
            peer_id=peer_id,
            last_beat_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            current_jobs=0,
            reputation=0.5,
            binary_hash="testhash",
            client_version="0.0.0",
        ))
        await db.commit()
    session_factory.track_peer(peer_id)

    conn = PeerConnection(
        peer_id=peer_id, session_id=uuid.uuid4(), ws=_WS(), member_id=uuid.uuid4()
    )
    await register_peer(conn)
    try:
        reaped = await reap_stale_peers(session_factory.factory)
        assert reaped == 0
        assert await get_peer(peer_id) is conn
    finally:
        await unregister_peer(peer_id)


@pytest.mark.asyncio
async def test_reaper_respects_reconnect_race_session_guard(session_factory) -> None:
    """A peer that reconnects (fresh session_id, fresh heartbeat) between
    the reaper's snapshot and its drain/unregister action must be left
    alone -- the session_id guard makes the reap a no-op instead of
    evicting the NEW connection."""
    from coordinator.services import peer_reaper

    peer_id = uuid.uuid4()
    async with session_factory.factory() as db:
        peer = Peer(member_id=uuid.uuid4(), public_key=uuid.uuid4().hex, status="active")
        db.add(peer)
        await db.flush()
        peer_id = peer.id
        db.add(PeerHeartbeat(
            peer_id=peer_id,
            last_beat_at=datetime.now(timezone.utc)
            - timedelta(seconds=HEARTBEAT_TTL_SECONDS + 15),
            current_jobs=0,
            reputation=0.5,
            binary_hash="testhash",
            client_version="0.0.0",
        ))
        await db.commit()
    session_factory.track_peer(peer_id)

    old_conn = PeerConnection(
        peer_id=peer_id, session_id=uuid.uuid4(), ws=_WS(), member_id=uuid.uuid4()
    )
    await register_peer(old_conn)

    # Monkeypatch online_peer_sessions to return the STALE snapshot (as if
    # taken before the reconnect below), independent of timing.
    from coordinator.services import peer_registry as pr

    stale_snapshot = {peer_id: old_conn.session_id}

    async def _fake_snapshot():
        return stale_snapshot

    orig = pr.online_peer_sessions
    pr.online_peer_sessions = _fake_snapshot
    try:
        # Reconnect happens "after" the snapshot was taken.
        new_conn = PeerConnection(
            peer_id=peer_id, session_id=uuid.uuid4(), ws=_WS(), member_id=uuid.uuid4()
        )
        await register_peer(new_conn)

        reaped = await peer_reaper.reap_stale_peers(session_factory.factory)
        assert reaped == 0, "reap must no-op when the snapshotted session is stale"
        assert await get_peer(peer_id) is new_conn
    finally:
        pr.online_peer_sessions = orig
        await unregister_peer(peer_id, session_id=new_conn.session_id)


# ---------------------------------------------------------------------------
# 5. Capacity hard-exclude.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saturated_peer_hard_excluded_even_with_higher_score(
    db_session: AsyncSession,
) -> None:
    member = uuid.uuid4()
    # `saturated` would win on score alone (higher tps) but is capped at 1
    # concurrent job and already has one in flight.
    saturated = await _mk_peer(
        db_session, member, tps=100.0, current_jobs=0, reputation=0.5,
        max_concurrent_jobs=1,
    )
    roomy = await _mk_peer(
        db_session, member, tps=1.0, current_jobs=0, reputation=0.5,
    )
    job_id = uuid.uuid4()
    await register_job(job_id, peer_id=saturated)
    try:
        assert await live_in_flight_count(saturated) == 1
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == roomy, (
            "a peer at its max_concurrent_jobs cap must be hard-excluded "
            "from candidacy, not merely score-dampened."
        )
    finally:
        await complete_job(job_id)
        await unregister_peer(saturated)
        await unregister_peer(roomy)


@pytest.mark.asyncio
async def test_capacity_cap_zero_takes_peer_out_of_candidacy(
    db_session: AsyncSession,
) -> None:
    """max_concurrent_jobs=0 is a valid operator drain knob: the peer is
    excluded from new-job candidacy without being disconnected."""
    member = uuid.uuid4()
    drained_peer = await _mk_peer(
        db_session, member, tps=50.0, max_concurrent_jobs=0,
    )
    try:
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is None
    finally:
        await unregister_peer(drained_peer)


@pytest.mark.asyncio
async def test_null_max_concurrent_jobs_is_uncapped(db_session: AsyncSession) -> None:
    """NULL (the default / every pre-migration row) means uncapped -- a
    peer with several jobs in flight and no cap set is still eligible."""
    member = uuid.uuid4()
    uncapped = await _mk_peer(db_session, member, tps=10.0)  # max_concurrent_jobs=None
    job_ids = [uuid.uuid4() for _ in range(5)]
    for j in job_ids:
        await register_job(j, peer_id=uncapped)
    try:
        assert await live_in_flight_count(uncapped) == 5
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == uncapped
    finally:
        for j in job_ids:
            await complete_job(j)
        await unregister_peer(uncapped)


@pytest.mark.asyncio
async def test_capacity_cap_releases_once_a_job_completes(
    db_session: AsyncSession,
) -> None:
    """The exclusion is dynamic against the LIVE counter, not a one-time
    snapshot -- completing a job frees a slot immediately."""
    member = uuid.uuid4()
    peer_id = await _mk_peer(db_session, member, tps=10.0, max_concurrent_jobs=1)
    job_id = uuid.uuid4()
    await register_job(job_id, peer_id=peer_id)
    try:
        # Saturated -> no eligible peer at all (only candidate).
        assert await pick_peer_for_model(db_session, _MODEL) is None

        await complete_job(job_id)

        # Slot freed -> eligible again.
        picked = await pick_peer_for_model(db_session, _MODEL)
        assert picked is not None and picked.peer_id == peer_id
    finally:
        await unregister_peer(peer_id)
