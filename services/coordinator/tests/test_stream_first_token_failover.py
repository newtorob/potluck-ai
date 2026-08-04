"""Active first-token failover on the STREAMING job endpoint.

`_dispatch_stream_with_failover` (routers/jobs.py) resolves a peer that has
actually produced a first token (or an empty-but-complete result) BEFORE
`dispatch_job_stream` hands a StreamingResponse to ASGI. A peer that hangs,
black-holes, or errors before its first token is excluded and the next-best
peer is tried instead -- entirely invisible to the client, who never sees a
byte from the dead peer.

These tests exercise the real dispatch path (`_dispatch_to_peer` ->
`select_peer_for_request` -> `pick_peer_for_model`, real Job rows, real
`peer_registry` state) with a stub peer WebSocket whose `send_json` schedules
a background coroutine that pokes the JobBuffer the way the real
`routers/peers.py` message handlers would -- so `exclude_peer_ids` threading,
`complete_job` idempotency, and `_finalize_job_in_db`'s credit-gating all run
for real, not mocked out.

Conventions:
  - `init_db()`/`get_session_factory()` + manual cleanup, matching
    test_finalize_idempotency.py -- `_dispatch_to_peer` calls `db.commit()`
    on the passed-in session, which is incompatible with the single-rollback
    `db_session` fixture used elsewhere.
  - `FIRST_TOKEN_DEADLINE_S` is monkeypatched to a small value so DOA/timeout
    scenarios don't actually burn wall-clock seconds per test.
  - Peers are owned by the SAME member_id as the requester ("My Machines"
    tier, scope="pool" tries this first) so routing doesn't depend on the
    open-pool reputation gate -- keeps the fixtures focused on failover, not
    trust-gating (out of scope per the task's scope note).
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.db.database import close_db, get_session_factory, init_db
from coordinator.models.credit import CreditLedger
from coordinator.models.job import Job
from coordinator.models.peer import Peer, PeerCapability, PeerHeartbeat
from coordinator.routers import jobs as jobs_router
from coordinator.routers.jobs import JobRequest, _dispatch_stream_with_failover
from coordinator.services.peer_registry import (
    PeerConnection,
    complete_job,
    get_job,
    register_peer,
    unregister_peer,
)

_MODEL = "qwen3:4b-first-token-failover-test"

BufferBehavior = Callable[["JobBufferLike"], Awaitable[None]]


class _WS:
    """Peer WS stub. `behavior`, if given, is scheduled as a background task
    the moment `send_json` fires (mirrors the real world: the peer's
    JobAssignedMessage delivery is what kicks off its eventual response) --
    it pokes the JobBuffer the way `routers/peers.py`'s job_token /
    job_complete / job_error handlers would.
    """

    def __init__(self, behavior: BufferBehavior | None = None) -> None:
        self.behavior = behavior
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)
        if self.behavior is not None:
            job_id = uuid.UUID(payload["job_id"])
            buf = await get_job(job_id)
            assert buf is not None
            asyncio.create_task(self.behavior(buf))


async def _never_respond(buf) -> None:  # noqa: ANN001 - JobBuffer, avoid import cycle
    """Simulates a hung/black-holed peer: does nothing. The caller's
    FIRST_TOKEN_DEADLINE_S must elapse for this attempt to resolve."""


async def _stream_one_token(buf, *, delay: float = 0.02, text: str = "hi") -> None:
    await asyncio.sleep(delay)
    buf.tokens.append(text)
    buf.token_stream.put_nowait(text)


async def _empty_complete(buf, *, delay: float = 0.02) -> None:
    await asyncio.sleep(delay)
    buf.finish_reason = "stop"
    await complete_job(buf.job_id)


async def _error_before_token(
    buf, *, delay: float = 0.02, category: str = "inference_failed"
) -> None:
    await asyncio.sleep(delay)
    buf.error_category = category
    buf.error_message = "peer blew up before generating anything"
    await complete_job(buf.job_id)


async def _mk_peer(
    db: AsyncSession,
    member_id: uuid.UUID,
    *,
    behavior: BufferBehavior | None,
    reputation: float = 0.5,
) -> tuple[uuid.UUID, _WS]:
    peer = Peer(member_id=member_id, public_key=uuid.uuid4().hex, status="active")
    db.add(peer)
    await db.flush()
    db.add(PeerCapability(
        peer_id=peer.id, model_slug=_MODEL, quantization="q4_K_M", tokens_per_second=10.0
    ))
    db.add(PeerHeartbeat(
        peer_id=peer.id,
        last_beat_at=datetime.now(timezone.utc),
        current_jobs=0,
        reputation=reputation,
        binary_hash="testhash",
        client_version="0.0.0",
    ))
    await db.commit()

    ws = _WS(behavior)
    await register_peer(
        PeerConnection(peer_id=peer.id, session_id=uuid.uuid4(), ws=ws, member_id=member_id)
    )
    return peer.id, ws


async def _cleanup(peer_ids: list[uuid.UUID], job_ids: list[uuid.UUID]) -> None:
    factory = get_session_factory()
    async with factory() as db:
        async with db.begin():
            if job_ids:
                await db.execute(delete(CreditLedger).where(CreditLedger.job_id.in_(job_ids)))
                await db.execute(delete(Job).where(Job.id.in_(job_ids)))
            if peer_ids:
                await db.execute(delete(PeerCapability).where(PeerCapability.peer_id.in_(peer_ids)))
                await db.execute(delete(PeerHeartbeat).where(PeerHeartbeat.peer_id.in_(peer_ids)))
                await db.execute(delete(Peer).where(Peer.id.in_(peer_ids)))
    for pid in peer_ids:
        await unregister_peer(pid)
    await close_db()


@pytest.fixture()
def fast_deadline(monkeypatch):
    """Shrink FIRST_TOKEN_DEADLINE_S so DOA/timeout scenarios resolve in
    ~150ms instead of the production 5s."""
    monkeypatch.setattr(jobs_router, "FIRST_TOKEN_DEADLINE_S", 0.15)
    return 0.15


async def _all_job_ids_for_peers(db: AsyncSession, peer_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    rows = (await db.execute(select(Job.id).where(Job.peer_id.in_(peer_ids)))).all()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# 1. First peer never sends a token within the deadline -> second peer
#    serves; first peer got no credit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_peer_serves_when_first_hangs(fast_deadline) -> None:
    await init_db()
    factory = get_session_factory()
    member_id = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    try:
        async with factory() as db:
            # `pick_peer_for_model`'s tiebreak (equal score) prefers the
            # MOST-RECENTLY-registered peer, so register `good` first and
            # `dead` second -- this deterministically makes `dead` win
            # attempt 1, proving failover (not lucky ordering) is what
            # lands the request on `good`.
            good_id, good_ws = await _mk_peer(db, member_id, behavior=_stream_one_token)
            dead_id, dead_ws = await _mk_peer(db, member_id, behavior=_never_respond)
            peer_ids = [dead_id, good_id]

            req = JobRequest(prompt="hello", model_slug=_MODEL)
            peer_conn, job, buf, served_scope, first_token = await _dispatch_stream_with_failover(
                db, req, member_id
            )

            assert first_token == "hi"
            assert peer_conn.peer_id == good_id, "failover must land on the peer that actually streamed"
            assert dead_ws.sent and len(dead_ws.sent) == 1, "dead peer should have been tried exactly once"
            assert good_ws.sent and len(good_ws.sent) == 1

            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == 2, "both attempts must have created a Job row"

            # The dead peer's attempt was finalized as failed, no credit.
            dead_job = (
                await db.execute(select(Job).where(Job.peer_id == dead_id))
            ).scalar_one()
            # _finalize_abandoned_attempt runs fire-and-forget-free (awaited
            # inline) inside _dispatch_stream_with_failover, so by the time
            # it returns the row is already settled.
            await db.refresh(dead_job)
            assert dead_job.status == "failed"
            assert dead_job.output_tokens in (0, None)

            credit_count = await db.scalar(
                select(func.count()).where(CreditLedger.job_id == dead_job.id)
            )
            assert credit_count == 0, "a peer that never produced output must earn NO credit"

            # Clean up the committed job's in-memory buffer/registry slot so
            # teardown doesn't see a dangling in-flight job.
            await complete_job(job.id)
    finally:
        job_ids_cleanup = []
        try:
            factory2 = get_session_factory()
            async with factory2() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)


# ---------------------------------------------------------------------------
# 2. Exhaust MAX_DISPATCH_ATTEMPTS dead peers -> 503.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausting_all_attempts_raises_503(fast_deadline) -> None:
    await init_db()
    factory = get_session_factory()
    member_id = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    try:
        async with factory() as db:
            for _ in range(jobs_router.MAX_DISPATCH_ATTEMPTS):
                pid, _ws = await _mk_peer(db, member_id, behavior=_never_respond)
                peer_ids.append(pid)

            req = JobRequest(prompt="hello", model_slug=_MODEL)
            with pytest.raises(HTTPException) as exc_info:
                await _dispatch_stream_with_failover(db, req, member_id)
            assert exc_info.value.status_code == 503

            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == jobs_router.MAX_DISPATCH_ATTEMPTS, (
                "exactly MAX_DISPATCH_ATTEMPTS Job rows must have been created -- "
                "no more, no fewer (invariant #4)."
            )

            # `_finalize_abandoned_attempt` wrote through a FRESH session
            # (`_finalize_job_in_db` always opens its own); `db`'s identity
            # map still holds the pre-finalize in-memory Job objects
            # (expire_on_commit=False on this factory), so force a reload
            # from the DB to see the committed status.
            db.expire_all()
            jobs_rows = (
                await db.execute(select(Job).where(Job.id.in_(job_ids)))
            ).scalars().all()
            for j in jobs_rows:
                assert j.status == "failed"
                assert j.output_tokens in (0, None)

            credit_count = await db.scalar(
                select(func.count()).where(CreditLedger.job_id.in_(job_ids))
            )
            assert credit_count == 0
    finally:
        job_ids_cleanup = []
        try:
            factory2 = get_session_factory()
            async with factory2() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)


# ---------------------------------------------------------------------------
# 3. A peer that DID stream a token then dies mid-stream is NOT retried.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_committed_peer_is_not_retried_after_mid_stream_death(fast_deadline) -> None:
    await init_db()
    factory = get_session_factory()
    member_id = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    try:
        async with factory() as db:
            # `first` streams one token then (simulated) dies -- error lands
            # AFTER the token, i.e. after commitment.
            async def _stream_then_die(buf) -> None:
                await asyncio.sleep(0.01)
                buf.tokens.append("partial")
                buf.token_stream.put_nowait("partial")
                await asyncio.sleep(0.05)
                buf.error_category = "peer_disconnected"
                buf.error_message = "connection dropped mid-stream"
                await complete_job(buf.job_id)

            # Register `second` first so `first` (more recently registered)
            # deterministically wins the tie-break on attempt 1 -- see the
            # ordering note in test_second_peer_serves_when_first_hangs.
            second_id, second_ws = await _mk_peer(db, member_id, behavior=_stream_one_token)
            first_id, first_ws = await _mk_peer(db, member_id, behavior=_stream_then_die)
            peer_ids = [first_id, second_id]

            req = JobRequest(prompt="hello", model_slug=_MODEL)
            peer_conn, job, buf, served_scope, first_token = await _dispatch_stream_with_failover(
                db, req, member_id
            )

            assert first_token == "partial"
            assert peer_conn.peer_id == first_id, "must commit to the peer that produced the token"

            # Let the simulated mid-stream death actually happen.
            await asyncio.sleep(0.1)
            assert buf.error_category == "peer_disconnected"

            # The second peer was NEVER dispatched to -- no JobAssignedMessage,
            # no Job row. Once a token has been yielded/committed there is no
            # retry, ever, even though the committed peer subsequently failed.
            assert second_ws.sent == []
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == 1
            assert job_ids[0] == job.id

            await complete_job(job.id)
    finally:
        job_ids_cleanup = []
        try:
            factory2 = get_session_factory()
            async with factory2() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)


# ---------------------------------------------------------------------------
# 4. Empty-but-complete first peer is not failed over.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_but_complete_first_peer_is_not_failed_over(fast_deadline) -> None:
    await init_db()
    factory = get_session_factory()
    member_id = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    try:
        async with factory() as db:
            # Register `never_used` first so `empty` (more recently
            # registered) deterministically wins the tie-break on attempt 1.
            never_used_id, never_used_ws = await _mk_peer(db, member_id, behavior=_stream_one_token)
            empty_id, empty_ws = await _mk_peer(db, member_id, behavior=_empty_complete)
            peer_ids = [empty_id, never_used_id]

            req = JobRequest(prompt="", model_slug=_MODEL)
            peer_conn, job, buf, served_scope, first_token = await _dispatch_stream_with_failover(
                db, req, member_id
            )

            assert first_token is None
            assert peer_conn.peer_id == empty_id, (
                "an empty-but-VALID completion must be treated as success, "
                "not failed over to the next peer."
            )
            assert never_used_ws.sent == [], "the second peer must never have been dispatched to"

            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == 1

            await complete_job(job.id)
    finally:
        job_ids_cleanup = []
        try:
            factory2 = get_session_factory()
            async with factory2() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)


# ---------------------------------------------------------------------------
# 5. Excluded peer never re-picked.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_excluded_peer_never_repicked_across_attempts(fast_deadline) -> None:
    await init_db()
    factory = get_session_factory()
    member_id = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    try:
        async with factory() as db:
            # Two dead peers, then a good one -- exercises the exclusion set
            # growing across >1 retries. Registration order is REVERSED
            # relative to intended pick order: the tie-break prefers the
            # MOST-RECENTLY-registered peer, so register `good` first,
            # `dead_b` next, `dead_a` last -- making `dead_a` deterministically
            # win attempt 1, `dead_b` attempt 2, `good` attempt 3.
            good_id, ws_good = await _mk_peer(db, member_id, behavior=_stream_one_token)
            dead_b, ws_b = await _mk_peer(db, member_id, behavior=_never_respond)
            dead_a, ws_a = await _mk_peer(db, member_id, behavior=_error_before_token)
            peer_ids = [dead_a, dead_b, good_id]

            req = JobRequest(prompt="hello", model_slug=_MODEL)
            peer_conn, job, buf, served_scope, first_token = await _dispatch_stream_with_failover(
                db, req, member_id
            )

            assert first_token == "hi"
            assert peer_conn.peer_id == good_id

            # Each dead peer was dispatched to EXACTLY once -- never re-picked
            # on a later attempt within the same request.
            assert len(ws_a.sent) == 1
            assert len(ws_b.sent) == 1
            assert len(ws_good.sent) == 1

            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == 3, "one Job row per attempt, three attempts total"

            await complete_job(job.id)
    finally:
        job_ids_cleanup = []
        try:
            factory2 = get_session_factory()
            async with factory2() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)
