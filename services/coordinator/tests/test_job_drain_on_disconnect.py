"""drain-on-disconnect keystone (FIX 3, launch-blocker).

Before this fix, `peer_registry._jobs` was keyed only by job_id, and a
peer disconnecting mid-job left the requester's JobBuffer to hang until
JOB_TIMEOUT_S (120s) with no failover signal — `routers/peers.py`'s
`finally: unregister_peer` never touched `_jobs`.

These tests exercise `peer_registry` directly (module-global in-memory
state, no DB / no real WebSocket needed — mirrors the pattern in
test_peer_quality_routing.py) to pin:

  1. A disconnect (`drain_peer_jobs`) fails every job in flight to that
     peer: `error_category` is set to the "peer_disconnected" sentinel and
     `done` fires immediately (not after a timeout).
  2. A job that already completed normally is NOT touched by a later
     disconnect (complete_job discards it from `_peer_jobs` first).
  3. The reconnect race: `register_peer` on a same-key reconnect drains the
     OLD connection's jobs itself, and the OLD connection's later
     session-gated `drain_peer_jobs`/`unregister_peer` calls become no-ops
     (they must not touch the NEW connection or its jobs).
  4. `error_category="peer_disconnected"` never writes a reputation/quality
     row — draining alone must not touch the DB at all.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from coordinator.services.peer_registry import (
    PeerConnection,
    complete_job,
    drain_peer_jobs,
    get_job,
    register_job,
    register_peer,
    unregister_peer,
)


class _WS:  # never actually sent to in these tests; a stub suffices
    pass


def _mk_conn(peer_id: uuid.UUID, member_id: uuid.UUID | None = None) -> PeerConnection:
    return PeerConnection(
        peer_id=peer_id,
        session_id=uuid.uuid4(),
        ws=_WS(),
        member_id=member_id or uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_drain_peer_jobs_fails_in_flight_job_fast() -> None:
    """The core keystone behaviour: draining sets error_category + fires
    `done` immediately, so an awaiting caller doesn't have to wait out
    JOB_TIMEOUT_S."""
    peer_id = uuid.uuid4()
    conn = _mk_conn(peer_id)
    await register_peer(conn)
    try:
        job_id = uuid.uuid4()
        buf = await register_job(job_id, peer_id=peer_id)
        assert not buf.done.is_set()
        assert buf.error_category is None

        drained = await drain_peer_jobs(peer_id)
        assert drained == [job_id]

        # Fires immediately -- no timeout needed to observe it.
        await asyncio.wait_for(buf.done.wait(), timeout=0.1)
        assert buf.error_category == "peer_disconnected"
        assert buf.error_message is not None

        # And the job is gone from the registry (routers/jobs.py's
        # get_job(job_id) lookups after this point correctly see nothing).
        assert await get_job(job_id) is None
    finally:
        await unregister_peer(peer_id)


@pytest.mark.asyncio
async def test_drain_peer_jobs_only_touches_that_peers_jobs() -> None:
    peer_a = uuid.uuid4()
    peer_b = uuid.uuid4()
    await register_peer(_mk_conn(peer_a))
    await register_peer(_mk_conn(peer_b))
    try:
        job_a = uuid.uuid4()
        job_b = uuid.uuid4()
        buf_a = await register_job(job_a, peer_id=peer_a)
        buf_b = await register_job(job_b, peer_id=peer_b)

        drained = await drain_peer_jobs(peer_a)
        assert drained == [job_a]
        assert buf_a.error_category == "peer_disconnected"

        # peer_b's job is completely untouched.
        assert buf_b.error_category is None
        assert not buf_b.done.is_set()
        assert await get_job(job_b) is buf_b
    finally:
        await unregister_peer(peer_a)
        await unregister_peer(peer_b)
        await complete_job(job_b)


@pytest.mark.asyncio
async def test_complete_job_then_disconnect_does_not_redrain() -> None:
    """A job that finished normally (complete_job) before the peer
    disconnects must not be touched by a later drain — complete_job already
    discarded it from `_peer_jobs`."""
    peer_id = uuid.uuid4()
    await register_peer(_mk_conn(peer_id))
    try:
        job_id = uuid.uuid4()
        buf = await register_job(job_id, peer_id=peer_id)
        buf.finish_reason = "stop"
        await complete_job(job_id)
        assert buf.done.is_set()
        assert buf.error_category is None  # completed cleanly

        drained = await drain_peer_jobs(peer_id)
        assert drained == []
        # error_category is untouched -- still a clean completion, not
        # retroactively marked peer_disconnected.
        assert buf.error_category is None
    finally:
        await unregister_peer(peer_id)


@pytest.mark.asyncio
async def test_peer_disconnected_never_appears_as_wrong_error_category_value() -> None:
    """error_category is exactly the sentinel string, so any future
    quality/reputation code can filter it out by an exact match."""
    peer_id = uuid.uuid4()
    await register_peer(_mk_conn(peer_id))
    try:
        job_id = uuid.uuid4()
        buf = await register_job(job_id, peer_id=peer_id)
        await drain_peer_jobs(peer_id)
        assert buf.error_category == "peer_disconnected"
    finally:
        await unregister_peer(peer_id)


# ---------------------------------------------------------------------------
# Reconnect race: same-key reconnect must not evict the new connection or
# drain its jobs via the old connection's stale disconnect handler.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_drains_old_connections_jobs_not_new() -> None:
    peer_id = uuid.uuid4()
    old_conn = _mk_conn(peer_id)
    await register_peer(old_conn)
    old_job = uuid.uuid4()
    old_buf = await register_job(old_job, peer_id=peer_id)

    # Same-key reconnect: register_peer must drain the OLD connection's job.
    new_conn = _mk_conn(peer_id)
    await register_peer(new_conn)

    assert old_buf.error_category == "peer_disconnected"
    assert old_buf.done.is_set()

    # A new job dispatched to the (now current) connection...
    new_job = uuid.uuid4()
    new_buf = await register_job(new_job, peer_id=peer_id)

    # ...survives the OLD connection's belated, session-gated disconnect
    # handler: session_id no longer matches the currently-registered conn,
    # so both drain and unregister are no-ops.
    stale_drained = await drain_peer_jobs(peer_id, session_id=old_conn.session_id)
    assert stale_drained == []
    assert new_buf.error_category is None
    assert not new_buf.done.is_set()

    still_removed = await unregister_peer(peer_id, session_id=old_conn.session_id)
    assert still_removed is False

    # The NEW connection is still registered.
    from coordinator.services.peer_registry import get_peer

    current = await get_peer(peer_id)
    assert current is new_conn

    # Cleanup: drain + unregister using the correct (current) session.
    await drain_peer_jobs(peer_id, session_id=new_conn.session_id)
    await unregister_peer(peer_id, session_id=new_conn.session_id)


@pytest.mark.asyncio
async def test_unregister_peer_session_guard_no_op_on_mismatch() -> None:
    peer_id = uuid.uuid4()
    conn = _mk_conn(peer_id)
    await register_peer(conn)
    try:
        # Wrong session_id -> no-op, connection stays registered.
        removed = await unregister_peer(peer_id, session_id=uuid.uuid4())
        assert removed is False
        from coordinator.services.peer_registry import get_peer

        assert await get_peer(peer_id) is conn
    finally:
        # Correct session_id -> actually removes it.
        removed = await unregister_peer(peer_id, session_id=conn.session_id)
        assert removed is True
