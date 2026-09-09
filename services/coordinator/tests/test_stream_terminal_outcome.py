"""One terminal outcome per job (audit finding 5).

Every path out of a dispatched job must settle the `jobs` row into exactly
one terminal state, and only a genuinely COMPLETED job may mint credits.
The bugs these tests pin down:

  a. Stream timeout emitted an error envelope AND a meta envelope AND
     [DONE], and the finalizer saw `error_category is None`, so the row was
     recorded "completed" and minted credits for work that never finished.
  b. Client disconnect mid-stream recorded "completed" for the same reason,
     and spawned the finalize task only after an `await`, so a contended
     lock during cancellation could skip finalization entirely.
  c. A `ws.send_json` failure left the row at "dispatched" forever and blew
     the streaming retry budget (the 502 escaped the failover loop).
  d. The sync POST /jobs timeout branch raised 504 without touching the row.
  e. A flat 5s first-token deadline fails over any peer that needs longer to
     process a long prompt (a split 32B model measured ~22s to first token
     on a 2048-token prompt).

Conventions match tests/test_stream_first_token_failover.py: real dispatch
path, real Job rows, real peer_registry state, stub peer WebSockets.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.db.database import close_db, get_db, get_session_factory, init_db
from coordinator.middleware.session_auth import resolve_session_member_id
from coordinator.models.credit import CreditLedger
from coordinator.models.job import Job
from coordinator.models.peer import Peer, PeerCapability, PeerHeartbeat
from coordinator.routers import jobs as jobs_router
from coordinator.routers.jobs import JobRequest
from coordinator.services.peer_registry import (
    PeerConnection,
    complete_job,
    register_peer,
)

# Fixture reuse: _WS (peer WS stub whose send_json schedules a buffer-poking
# behavior), _mk_peer, _cleanup, _all_job_ids_for_peers, _never_respond and
# the fast_deadline monkeypatch all come from the failover suite unchanged.
from tests.test_stream_first_token_failover import (  # noqa: F401 - fixture re-export
    _MODEL,
    _WS,
    _all_job_ids_for_peers,
    _cleanup,
    _mk_peer,
    _never_respond,
    fast_deadline,
)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


class _BrokenWS(_WS):
    """Peer WS stub whose send_json always raises: the socket looked alive to
    the registry but is dead by the time the JobAssignedMessage goes out
    (DOA mode 3). Records the attempt first so the test can prove the peer
    was tried exactly once."""

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)
        raise RuntimeError("peer socket closed before the assignment landed")


async def _mk_peer_with_ws(
    db: AsyncSession,
    member_id: uuid.UUID,
    ws,
    *,
    reputation: float = 0.5,
) -> uuid.UUID:
    """Same fixture shape as `_mk_peer` but takes a caller-supplied WS stub."""
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
    await register_peer(
        PeerConnection(peer_id=peer.id, session_id=uuid.uuid4(), ws=ws, member_id=member_id)
    )
    return peer.id


def _make_app(factory, requester: uuid.UUID) -> FastAPI:
    async def request_db():
        async with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(jobs_router.router, prefix="/jobs")
    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[resolve_session_member_id] = lambda: requester
    return app


async def _drain_background_tasks() -> None:
    pending = list(jobs_router._background_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _credit_rows(db: AsyncSession, job_ids: list[uuid.UUID]) -> int:
    if not job_ids:
        return 0
    return await db.scalar(
        select(func.count()).where(CreditLedger.job_id.in_(job_ids))
    )


# ---------------------------------------------------------------------------
# a. Stream timeout: error envelope, DONE, no meta, row failed, no credits.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_timeout_is_terminal_and_mints_nothing(
    monkeypatch, fast_deadline
) -> None:
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    # Cross-member peer: a job recorded "completed" here WOULD mint credits,
    # which is exactly the bug. Same-member would hide it behind the
    # self-served ledger skip.
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    monkeypatch.setattr(jobs_router, "JOB_TIMEOUT_S", 0.3)

    async def _one_token_then_stall(buf) -> None:
        # Peer streams a first token (so the failover loop commits to it),
        # reports plausible-looking totals, then hangs forever: `done` is
        # never set, so the generator's JOB_TIMEOUT_S must fire.
        buf.tokens.append("tick")
        buf.output_tokens = 7
        buf.duration_ms = 11
        buf.token_stream.put_nowait("tick")

    try:
        async with factory() as db:
            pid, _ws = await _mk_peer(db, contributor, behavior=_one_token_then_stall)
            peer_ids.append(pid)

        app = _make_app(factory, requester)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/jobs/stream", json={
                "prompt": "stall after one token", "model_slug": _MODEL,
            })

        assert response.status_code == 200
        body = response.text
        assert '"category": "timeout"' in body, (
            "the JOB_TIMEOUT_S branch must emit a timeout error envelope"
        )
        err_idx = body.index('"error"')
        assert '"meta"' not in body[err_idx:], (
            "a meta envelope must NEVER follow an error envelope: a naive "
            "client would read it as a successful completion"
        )
        done_idx = body.index("data: [DONE]")
        assert done_idx > err_idx, "DONE must terminate the stream after the error"

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == 1
            db_job = await db.get(Job, job_ids[0])
            assert db_job.status == "failed"
            assert getattr(db_job, "terminal_reason", None) == "timeout"
            assert await _credit_rows(db, job_ids) == 0, (
                "a timed-out job must never mint credits"
            )
    finally:
        await _drain_background_tasks()
        job_ids_cleanup: list[uuid.UUID] = []
        try:
            async with get_session_factory()() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)


# ---------------------------------------------------------------------------
# b. Client aborts mid-stream: row failed/cancelled, no credits, finalize
#    task spawned before the first await in the finally.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_abort_mid_stream_records_cancelled(fast_deadline) -> None:
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    async def _stream_then_wait(buf) -> None:
        buf.tokens.append("partial")
        buf.output_tokens = 42
        buf.duration_ms = 99
        buf.token_stream.put_nowait("partial")

    try:
        async with factory() as db:
            pid, _ws = await _mk_peer(db, contributor, behavior=_stream_then_wait)
            peer_ids.append(pid)

            req = JobRequest(prompt="client will hang up", model_slug=_MODEL)
            response = await jobs_router.dispatch_job_stream(req, db, requester)

        agen = response.body_iterator
        first_chunk = await agen.asend(None)
        assert "partial" in first_chunk

        before = set(jobs_router._background_tasks)
        with pytest.raises(asyncio.CancelledError):
            await agen.athrow(asyncio.CancelledError)
        spawned = set(jobs_router._background_tasks) - before
        assert spawned or before, (
            "the finalize task must be spawned from the finally block even "
            "when the generator is cancelled"
        )

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == 1
            db_job = await db.get(Job, job_ids[0])
            assert db_job.status == "failed"
            assert getattr(db_job, "terminal_reason", None) == "cancelled"
            assert await _credit_rows(db, job_ids) == 0, (
                "an aborted stream must never mint credits"
            )
    finally:
        await _drain_background_tasks()
        job_ids_cleanup: list[uuid.UUID] = []
        try:
            async with get_session_factory()() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)


# ---------------------------------------------------------------------------
# c. DOA mode 3: send_json raises -> failover continues, abandoned row is
#    settled as dispatch_failed, exactly one Job row per attempt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_fails_over_and_settles_the_row(fast_deadline) -> None:
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    async def _finish(buf) -> None:
        buf.tokens.append("second peer token")
        buf.output_tokens = 3
        buf.duration_ms = 5
        buf.token_stream.put_nowait("second peer token")
        buf.finish_reason = "stop"
        await complete_job(buf.job_id)

    try:
        async with factory() as db:
            # Equal scores tie-break to the MOST-recently-registered peer, so
            # register the good peer first and the broken one second: the
            # broken peer deterministically wins attempt 1.
            good_id, good_ws = await _mk_peer(db, contributor, behavior=_finish)
            broken_ws = _BrokenWS()
            broken_id = await _mk_peer_with_ws(db, contributor, broken_ws)
            peer_ids = [broken_id, good_id]

        app = _make_app(factory, requester)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/jobs/stream", json={
                "prompt": "first peer socket is dead", "model_slug": _MODEL,
            })

        assert response.status_code == 200, (
            "a dead socket on attempt 1 must not escape the failover loop as a 502"
        )
        assert "second peer token" in response.text
        assert len(broken_ws.sent) == 1, "the broken peer must be tried exactly once"
        assert len(good_ws.sent) == 1

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == 2, "exactly one Job row per dispatch attempt"

            abandoned = (
                await db.execute(select(Job).where(Job.peer_id == broken_id))
            ).scalar_one()
            assert abandoned.status == "failed", (
                "a job whose assignment never reached the peer must not be "
                "left at 'dispatched' forever"
            )
            assert getattr(abandoned, "terminal_reason", None) == "dispatch_failed"

            served = (
                await db.execute(select(Job).where(Job.peer_id == good_id))
            ).scalar_one()
            assert served.status == "completed"
            assert getattr(served, "terminal_reason", None) == "completed"

            assert await _credit_rows(db, [abandoned.id]) == 0
    finally:
        await _drain_background_tasks()
        job_ids_cleanup: list[uuid.UUID] = []
        try:
            async with get_session_factory()() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)


# ---------------------------------------------------------------------------
# d. Sync POST /jobs timeout settles the row before raising 504.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_job_timeout_settles_the_row(monkeypatch) -> None:
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    monkeypatch.setattr(jobs_router, "JOB_TIMEOUT_S", 0.3)

    try:
        async with factory() as db:
            pid, _ws = await _mk_peer(db, contributor, behavior=_never_respond)
            peer_ids.append(pid)

        app = _make_app(factory, requester)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/jobs", json={
                "prompt": "peer never answers", "model_slug": _MODEL,
            })

        assert response.status_code == 504

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == 1
            db_job = await db.get(Job, job_ids[0])
            assert db_job.status == "failed", (
                "the sync timeout branch must not leave the row at 'dispatched'"
            )
            assert getattr(db_job, "terminal_reason", None) == "timeout"
            assert await _credit_rows(db, job_ids) == 0
    finally:
        await _drain_background_tasks()
        job_ids_cleanup: list[uuid.UUID] = []
        try:
            async with get_session_factory()() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)


# ---------------------------------------------------------------------------
# e. Derived first-token deadline.
# ---------------------------------------------------------------------------

_DEADLINE_MODEL = "qwen3:32b-split-first-token-deadline-test"


async def _mk_deadline_peer(
    db: AsyncSession,
    *,
    hardware: dict | None = None,
    tokens_per_second: float | None = None,
    with_capability: bool = True,
) -> tuple[uuid.UUID, PeerConnection]:
    """A Peer row plus a PeerConnection that carries only ids, exactly like
    the live registry does. No WS registration: `_first_token_deadline` is a
    pure read of (request, peer ids) against the DB."""
    peer = Peer(
        member_id=uuid.uuid4(),
        public_key=uuid.uuid4().hex,
        status="active",
        hardware_summary=hardware if hardware is not None else {},
    )
    db.add(peer)
    await db.flush()
    if with_capability:
        db.add(PeerCapability(
            peer_id=peer.id,
            model_slug=_DEADLINE_MODEL,
            quantization="q4_K_M",
            tokens_per_second=tokens_per_second,
        ))
    await db.commit()
    conn = PeerConnection(
        peer_id=peer.id, session_id=uuid.uuid4(), ws=None, member_id=peer.member_id
    )
    return peer.id, conn


async def _cleanup_deadline_peers(peer_ids: list[uuid.UUID]) -> None:
    async with get_session_factory()() as db:
        async with db.begin():
            await db.execute(
                delete(PeerCapability).where(PeerCapability.peer_id.in_(peer_ids))
            )
            await db.execute(delete(Peer).where(Peer.id.in_(peer_ids)))
    await close_db()


@pytest.mark.asyncio
async def test_first_token_deadline_short_prompt_returns_the_floor() -> None:
    from coordinator.routers.jobs import _first_token_deadline

    await init_db()
    peer_ids: list[uuid.UUID] = []
    try:
        async with get_session_factory()() as db:
            pid, conn = await _mk_deadline_peer(
                db, hardware={"system": "Linux", "machine": "x86_64"},
                with_capability=False,
            )
            peer_ids.append(pid)
            req = JobRequest(prompt="hi", model_slug=_DEADLINE_MODEL)
            deadline = await _first_token_deadline(db, req, conn)

        assert deadline >= jobs_router.FIRST_TOKEN_DEADLINE_S
        assert deadline < jobs_router.FIRST_TOKEN_DEADLINE_S + 0.5, (
            "a two-character prompt must not meaningfully extend the floor"
        )
    finally:
        await _cleanup_deadline_peers(peer_ids)


@pytest.mark.asyncio
async def test_first_token_deadline_long_prompt_on_cpu_tier_peer() -> None:
    """A 2048-token prompt on a tier-1 (CPU proxy, score 1.0) peer must clear
    the ~22s a split 32B model actually measured, and stay under the cap."""
    from coordinator.routers.jobs import FIRST_TOKEN_MAX_S, _first_token_deadline

    await init_db()
    peer_ids: list[uuid.UUID] = []
    try:
        async with get_session_factory()() as db:
            pid, conn = await _mk_deadline_peer(
                db, hardware={"system": "Linux", "machine": "x86_64"},
                with_capability=False,
            )
            peer_ids.append(pid)
            # 4 chars ~= 1 token, so 8192 chars ~= 2048 prompt tokens.
            req = JobRequest(prompt="x" * 8192, model_slug=_DEADLINE_MODEL)
            deadline = await _first_token_deadline(db, req, conn)

        assert deadline > 22.0, (
            "a flat 5s deadline fails over a peer that needs ~22s to first "
            "token on a 2048-token prompt"
        )
        assert deadline <= FIRST_TOKEN_MAX_S == 90.0
    finally:
        await _cleanup_deadline_peers(peer_ids)


@pytest.mark.asyncio
async def test_first_token_deadline_uses_measured_tokens_per_second() -> None:
    from coordinator.routers.jobs import _first_token_deadline

    await init_db()
    peer_ids: list[uuid.UUID] = []
    try:
        async with get_session_factory()() as db:
            measured_id, measured_conn = await _mk_deadline_peer(
                db, hardware={"system": "Linux", "machine": "x86_64"},
                tokens_per_second=200.0,
            )
            tier_id, tier_conn = await _mk_deadline_peer(
                db, hardware={"system": "Linux", "machine": "x86_64"},
                with_capability=False,
            )
            peer_ids += [measured_id, tier_id]
            req = JobRequest(prompt="x" * 8192, model_slug=_DEADLINE_MODEL)
            measured = await _first_token_deadline(db, req, measured_conn)
            tier = await _first_token_deadline(db, req, tier_conn)

        # 2048 prompt tokens / 200 tok/s = 10.24s on top of the 5s floor.
        assert measured == pytest.approx(15.24, abs=0.05)
        assert measured < tier, (
            "a measured tokens_per_second must beat the coarse hardware-tier "
            "proxy when the capability row exists"
        )
    finally:
        await _cleanup_deadline_peers(peer_ids)


@pytest.mark.asyncio
async def test_first_token_deadline_counts_messages_and_clamps_at_max() -> None:
    from coordinator.routers.jobs import FIRST_TOKEN_MAX_S, _first_token_deadline
    from coordinator.schemas.peer import ChatMessage

    await init_db()
    peer_ids: list[uuid.UUID] = []
    try:
        async with get_session_factory()() as db:
            pid, conn = await _mk_deadline_peer(
                db, hardware={"system": "Linux", "machine": "x86_64"},
                tokens_per_second=1.0,
            )
            peer_ids.append(pid)
            req = JobRequest(
                prompt="",
                messages=[ChatMessage(role="user", content="y" * 40000)],
                model_slug=_DEADLINE_MODEL,
            )
            deadline = await _first_token_deadline(db, req, conn)

        assert deadline == FIRST_TOKEN_MAX_S, (
            "multi-turn history counts toward the estimate, and the estimate "
            "is clamped at FIRST_TOKEN_MAX_S"
        )
    finally:
        await _cleanup_deadline_peers(peer_ids)


@pytest.mark.asyncio
async def test_first_token_deadline_honors_monkeypatched_floor(fast_deadline) -> None:
    """The failover suite shrinks FIRST_TOKEN_DEADLINE_S to keep DOA tests
    fast. The derived deadline must read the module global at CALL time, not
    capture it at import."""
    from coordinator.routers.jobs import _first_token_deadline

    await init_db()
    peer_ids: list[uuid.UUID] = []
    try:
        async with get_session_factory()() as db:
            pid, conn = await _mk_deadline_peer(
                db, hardware={"system": "Linux", "machine": "x86_64"},
                tokens_per_second=10.0,
            )
            peer_ids.append(pid)
            req = JobRequest(prompt="hello", model_slug=_DEADLINE_MODEL)
            deadline = await _first_token_deadline(db, req, conn)

        assert deadline >= fast_deadline
        assert deadline < 1.0, (
            "an import-time capture of FIRST_TOKEN_DEADLINE_S would ignore "
            "the fixture and slow five existing failover tests to a crawl"
        )
    finally:
        await _cleanup_deadline_peers(peer_ids)


# ---------------------------------------------------------------------------
# f. Drain beats the deadline once the peer has finished.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_peer_backlog_is_drained_past_the_deadline(
    monkeypatch, fast_deadline
) -> None:
    """A peer that already signalled job_complete has delivered its answer;
    the remaining tokens are sitting in the coordinator's own queue.

    Reporting a timeout there throws away work that exists, tells the client
    its answer failed when most of it already arrived, and denies the
    contributor credit for a job it actually finished. The deadline is a
    patience budget for a peer that is still GENERATING, so once `done` is
    set the queue is drained (bounded by the queue size) even if the deadline
    has passed.
    """
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []
    job_id: uuid.UUID | None = None

    monkeypatch.setattr(jobs_router, "JOB_TIMEOUT_S", 0.1)

    async def _complete_with_a_backlog(buf) -> None:
        buf.tokens.extend(["head", "tail"])
        buf.output_tokens = 9
        buf.duration_ms = 77
        buf.finish_reason = "stop"
        buf.token_stream.put_nowait("head")
        buf.token_stream.put_nowait("tail")
        await complete_job(buf.job_id)

    try:
        async with factory() as db:
            pid, _ws = await _mk_peer(db, contributor, behavior=_complete_with_a_backlog)
            peer_ids.append(pid)
            req = JobRequest(prompt="complete but backlogged", model_slug=_MODEL)
            response = await jobs_router.dispatch_job_stream(req, db, requester)

        agen = response.body_iterator
        first = await agen.asend(None)
        assert "head" in first
        # Slow consumer: the deadline expires while "tail" is still queued
        # and buf.done is already set.
        await asyncio.sleep(0.25)
        rest = [chunk async for chunk in agen]
        body = first + "".join(rest)

        assert "tail" in body, (
            "the queued remainder of an answer the peer already finished must "
            "be delivered, not discarded"
        )
        assert '"category": "timeout"' not in body, (
            "a peer that reported job_complete did not time out"
        )
        assert '"meta"' in body
        assert body.count("[DONE]") == 1

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            job_id = job_ids[0]
            db_job = await db.get(Job, job_id)
            assert db_job.status == "completed"
            assert db_job.terminal_reason == "completed"
            assert db_job.output_tokens == 9
            assert await _credit_rows(db, job_ids) == 2, (
                "the contributor finished the job and it was delivered"
            )
    finally:
        await _drain_background_tasks()
        if job_id is not None:
            await complete_job(job_id)
        job_ids_cleanup: list[uuid.UUID] = []
        try:
            async with get_session_factory()() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)
