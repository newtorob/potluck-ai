"""Adversarial review of the single-terminal-outcome contract (routers/jobs.py).

The implementer's own suite (tests/test_stream_terminal_outcome.py plus the
outcome matrix in tests/test_finalize_idempotency.py) proves the happy shape of
each branch. This file attacks the seams BETWEEN the branches: the exact
suspension points where the wire state and the recorded outcome can disagree,
the ordering between the drain check and the deadline check, conflicting
double finalizes, and the one exit that runs no code at all.

Claimed contract under test:
  * one terminal outcome per job decides status, token/duration writes and the
    single credit-minting gate; only COMPLETED mints
  * terminal_reason + error_category persist on the row for every path
  * after an error envelope the wire carries DONE and never meta
  * a client disconnect finalizes as cancelled, and the finalize task is
    created before any await in the finally
  * a failed ws.send_json finalizes that attempt as dispatch_failed and the
    failover loop continues
  * the first-token deadline is derived, floored at the module global and
    capped at 90 s
  * no Job row is left at 'dispatched' once the handler has returned

Tests named `test_defect_*` FAIL against the current implementation and are the
finding. Tests named `test_characterizes_*` pin down behaviour that the
contract does not actually specify, so a future change to it is a deliberate
choice rather than an accident.

Fixtures are reused verbatim from tests/test_stream_first_token_failover.py and
tests/test_stream_terminal_outcome.py; nothing under coordinator/ and no
existing test is modified by this file.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from coordinator.db.database import close_db, get_session_factory, init_db
from coordinator.models.credit import CreditLedger
from coordinator.models.job import Job
from coordinator.routers import jobs as jobs_router
from coordinator.routers.jobs import JobRequest, Outcome, _finalize_job_in_db
from coordinator.schemas.peer import ChatMessage
from coordinator.services.peer_registry import (
    PeerConnection,
    complete_job,
    get_job,
)

# ---- Fixture reuse (no redefinition) ------------------------------------- #
from tests.test_stream_first_token_failover import (  # noqa: F401 - fixture re-export
    _MODEL,
    _all_job_ids_for_peers,
    _cleanup,
    _mk_peer,
    fast_deadline,
)
from tests.test_stream_terminal_outcome import (
    _BrokenWS,
    _DEADLINE_MODEL,
    _cleanup_deadline_peers,
    _credit_rows,
    _drain_background_tasks,
    _make_app,
    _mk_deadline_peer,
    _mk_peer_with_ws,
)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


async def _stream_one_token_and_hold(buf) -> None:  # noqa: ANN001 - JobBuffer
    """Peer produces its first token (so the failover loop commits to it) and
    then goes quiet without ever signalling completion."""
    buf.tokens.append("first")
    buf.output_tokens = 1
    buf.duration_ms = 10
    buf.token_stream.put_nowait("first")


async def _stream_and_complete(buf, *, text: str = "hello") -> None:  # noqa: ANN001
    """A perfectly ordinary successful job: one token, then job_complete."""
    buf.tokens.append(text)
    buf.output_tokens = 12
    buf.duration_ms = 34
    buf.finish_reason = "stop"
    buf.token_stream.put_nowait(text)
    await complete_job(buf.job_id)


async def _stream_two_tokens_and_hold(buf) -> None:  # noqa: ANN001
    """Two tokens, spaced, and never a completion: lets a test park the
    generator at a MID-LOOP token yield rather than the pre-loop one."""
    buf.tokens.append("one")
    buf.output_tokens = 2
    buf.duration_ms = 20
    buf.token_stream.put_nowait("one")
    await asyncio.sleep(0.02)
    buf.tokens.append("two")
    buf.token_stream.put_nowait("two")


async def _dispatched_count(db, peer_ids: list[uuid.UUID]) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.peer_id.in_(peer_ids), Job.status == "dispatched")
    )


def _envelopes(chunks: list[str]) -> list[dict]:
    """Decode the SSE payloads that are JSON (everything but [DONE])."""
    out: list[dict] = []
    for chunk in chunks:
        body = chunk.removeprefix("data: ").strip()
        if body and body != "[DONE]":
            out.append(json.loads(body))
    return out


# ===========================================================================
# Vector 1 + 9. The terminal_wire_sent guard, probed from both sides.
#
# The guard is set AFTER the meta/error envelope is yielded and BEFORE the
# DONE envelope. That leaves exactly one suspension point -- the meta yield --
# at which the client has the whole answer but the job is still recorded as
# though nothing finished.
# ===========================================================================


@pytest.mark.asyncio
async def test_aclose_at_the_done_yield_keeps_completed_and_credits(
    fast_deadline,
) -> None:
    """Vector 9a: the guard the implementer added. A client that hangs up while
    the generator is parked at `yield _sse_done()` already has its whole
    answer, so the job stays COMPLETED and the contributor is still paid."""
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []
    job_id: uuid.UUID | None = None

    try:
        async with factory() as db:
            pid, _ws = await _mk_peer(db, contributor, behavior=_stream_and_complete)
            peer_ids.append(pid)
            req = JobRequest(prompt="finish then hang up", model_slug=_MODEL)
            response = await jobs_router.dispatch_job_stream(req, db, requester)

        agen = response.body_iterator
        chunks = [await agen.asend(None) for _ in range(3)]
        assert "hello" in chunks[0]
        assert '"meta"' in chunks[1]
        assert "[DONE]" in chunks[2]

        # Parked on the DONE yield: the wire is complete.
        await agen.aclose()
        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            job_id = job_ids[0]
            db_job = await db.get(Job, job_id)
            assert db_job.status == "completed"
            assert db_job.terminal_reason == "completed"
            assert db_job.error_category is None
            assert db_job.output_tokens == 12
            assert db_job.duration_ms == 34
            assert await _credit_rows(db, job_ids) == 2, (
                "a fully delivered answer must still mint for the contributor"
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


@pytest.mark.asyncio
async def test_defect_aclose_at_the_meta_yield_relabels_a_completed_job(
    fast_deadline,
) -> None:
    """Vector 1 (completed side): the SAME fully delivered answer, closed one
    yield earlier.

    `terminal_wire_sent` is assigned after `yield _sse_meta(...)` returns, so a
    disconnect while the generator is parked ON the meta yield is
    indistinguishable, to the guard, from a disconnect halfway through the
    tokens. Every token and the meta summary have already been handed to the
    transport; the peer reported job_complete; and the job is still rewritten
    as cancelled/failed with the contributor paid nothing.

    Expected by the contract: one terminal outcome, decided by what actually
    happened -- COMPLETED.
    """
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []
    job_id: uuid.UUID | None = None

    try:
        async with factory() as db:
            pid, _ws = await _mk_peer(db, contributor, behavior=_stream_and_complete)
            peer_ids.append(pid)
            req = JobRequest(prompt="hang up right after meta", model_slug=_MODEL)
            response = await jobs_router.dispatch_job_stream(req, db, requester)

        agen = response.body_iterator
        token_chunk = await agen.asend(None)
        meta_chunk = await agen.asend(None)
        assert "hello" in token_chunk
        meta = _envelopes([meta_chunk])[0]["meta"]
        assert meta["output_tokens"] == 12 and meta["finish_reason"] == "stop", (
            "the client received the completion summary before hanging up"
        )

        # Parked on the meta yield. The client has the entire answer.
        await agen.aclose()
        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            job_id = job_ids[0]
            db_job = await db.get(Job, job_id)
            assert db_job.terminal_reason == "completed", (
                f"a job whose tokens AND meta summary reached the client was "
                f"recorded as {db_job.terminal_reason!r}"
            )
            assert db_job.status == "completed"
            assert await _credit_rows(db, job_ids) == 2, (
                "the contributor did the whole job and delivered it; the "
                "terminal_wire_sent guard covers the DONE yield but not the "
                "meta yield one step earlier, so the credit is silently lost"
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


@pytest.mark.asyncio
async def test_aclose_at_a_mid_loop_token_yield_cancels_and_mints_nothing(
    fast_deadline,
) -> None:
    """Vector 1 (cancelled side) + vector 9b: the guard must not leak the other
    way. A GeneratorExit at a token yield inside the pull loop -- not the
    pre-loop first-token yield the implementer's test covers -- has to record
    cancelled and mint nothing."""
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []
    job_id: uuid.UUID | None = None

    try:
        async with factory() as db:
            pid, _ws = await _mk_peer(
                db, contributor, behavior=_stream_two_tokens_and_hold
            )
            peer_ids.append(pid)
            req = JobRequest(prompt="abort mid loop", model_slug=_MODEL)
            response = await jobs_router.dispatch_job_stream(req, db, requester)

        agen = response.body_iterator
        first = await agen.asend(None)  # pre-loop yield of the stashed token
        second = await agen.asend(None)  # mid-loop yield
        assert "one" in first and "two" in second

        await agen.aclose()
        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            job_id = job_ids[0]
            db_job = await db.get(Job, job_id)
            assert db_job.status == "failed"
            assert db_job.terminal_reason == "cancelled"
            assert db_job.error_category == "client_disconnected"
            assert db_job.output_tokens is None and db_job.duration_ms is None
            assert await _credit_rows(db, job_ids) == 0, (
                "a half-delivered stream must never mint"
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


# ===========================================================================
# Vector 8. Cancellation BEFORE the first byte -- the exit that runs no code.
# ===========================================================================


@pytest.mark.asyncio
async def test_defect_disconnect_before_the_first_chunk_never_finalizes(
    fast_deadline,
) -> None:
    """Vector 8: the client goes away between the dispatch commit and the first
    yield.

    `_dispatch_stream_with_failover` has already committed the Job row as
    'dispatched' and registered the in-flight buffer, and it may have spent up
    to FIRST_TOKEN_MAX_S (90 s) doing so with no bytes on the wire -- so this
    is the single widest window in the whole path for a requester to give up.

    `dispatch_job_stream` then hands ASGI a StreamingResponse whose body is a
    *not yet started* async generator. Starlette's task group cancels
    `stream_response` before its first `async for`, so the generator body never
    runs: no `finally`, no finalize task, no `complete_job`. Both the raw
    aclose() and the real ASGI drive below take that path.

    Contract: "a client disconnect finalizes as cancelled" and no row stays
    'dispatched' once the handler has returned.
    """
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []
    started_job_ids: list[uuid.UUID] = []

    try:
        # ---- Path A: starlette closes a generator it never started. ---- #
        async with factory() as db:
            pid_a, _ws_a = await _mk_peer(
                db, contributor, behavior=_stream_one_token_and_hold
            )
            peer_ids.append(pid_a)
            req = JobRequest(prompt="gone before the first byte", model_slug=_MODEL)
            resp_a = await jobs_router.dispatch_job_stream(req, db, requester)
        await resp_a.body_iterator.aclose()

        # ---- Path B: the real ASGI call with an immediate http.disconnect,
        # i.e. exactly what uvicorn hands starlette for a client that is
        # already gone. ---- #
        async with factory() as db:
            pid_b, _ws_b = await _mk_peer(
                db, contributor, behavior=_stream_one_token_and_hold
            )
            peer_ids.append(pid_b)
            req = JobRequest(prompt="gone before the first byte", model_slug=_MODEL)
            resp_b = await jobs_router.dispatch_job_stream(req, db, requester)

        sent: list[dict] = []

        # `sleep(0)` models the one thing a real transport does that an inline
        # stub does not: yield at every send/receive. Without a checkpoint,
        # starlette's cancel scope has nowhere to interrupt `stream_response`
        # and the generator gets started anyway -- an artifact of the stub, not
        # of uvicorn.
        async def _send(message: dict) -> None:
            await asyncio.sleep(0)
            sent.append(message)

        async def _receive() -> dict:
            await asyncio.sleep(0)
            return {"type": "http.disconnect"}

        await resp_b(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "path": "/jobs/stream",
                "headers": [],
            },
            _receive,
            _send,
        )
        assert not any(
            m.get("type") == "http.response.body" and m.get("body") for m in sent
        ), "no body chunk should have reached a client that was already gone"

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            started_job_ids = list(job_ids)
            assert len(job_ids) == 2, "one committed Job row per disconnected request"

            observed = {}
            for jid in job_ids:
                db_job = await db.get(Job, jid)
                observed[str(jid)] = (db_job.status, db_job.terminal_reason)
            assert all(v == ("failed", "cancelled") for v in observed.values()), (
                f"a client that hung up before the first chunk left these rows "
                f"unsettled: {observed}. The Job row is committed as "
                f"'dispatched' inside _dispatch_to_peer, and there is no job "
                f"reaper, so nothing ever settles it"
            )
            assert await _dispatched_count(db, peer_ids) == 0
            assert await _credit_rows(db, job_ids) == 0

            leaked = [jid for jid in job_ids if (await get_job(jid)) is not None]
            assert leaked == [], (
                f"the in-flight registry slot for {leaked} was never released: "
                f"complete_job() lives in the generator's finally, which never "
                f"ran, so these count as phantom concurrent jobs against the "
                f"peer's routing score until it disconnects"
            )
    finally:
        await _drain_background_tasks()
        for jid in started_job_ids:
            await complete_job(jid)
        job_ids_cleanup: list[uuid.UUID] = []
        try:
            async with get_session_factory()() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)


# ===========================================================================
# Vector 2. The deadline beats a peer that already completed.
# ===========================================================================


@pytest.mark.asyncio
async def test_characterizes_completed_peer_backlog_is_drained_past_the_deadline(
    monkeypatch, fast_deadline
) -> None:
    """Vector 2: job_complete has already landed and the remaining tokens are
    still queued when the deadline expires. Contract (item D of the fix): a peer
    that already finished has its backlog drained even past the deadline, so the
    delivered answer is not discarded and relabeled a timeout. The row settles
    completed with the peer's honest totals and credits are minted.
    """
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []
    job_id: uuid.UUID | None = None

    monkeypatch.setattr(jobs_router, "JOB_TIMEOUT_S", 0.1)

    async def _complete_with_a_backlog(buf) -> None:  # noqa: ANN001
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
        # Slow consumer: the deadline expires while "tail" is still queued and
        # buf.done is already set.
        await asyncio.sleep(0.25)
        rest = [chunk async for chunk in agen]

        body = first + "".join(rest)
        assert '"category": "timeout"' not in body, (
            "a peer that reported job_complete is still reported as a timeout"
        )
        assert "tail" in body, (
            "the queued remainder of a COMPLETED answer is dropped rather than "
            "drained"
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
            assert db_job.output_tokens == 9, (
                "the peer's self-reported 9 tokens must not be written for a "
                "non-COMPLETED outcome"
            )
            assert db_job.duration_ms == 77
            assert await _credit_rows(db, job_ids) == 2
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


# ===========================================================================
# Vector 3. Peer error one tick after the deadline branch fired.
# ===========================================================================


@pytest.mark.asyncio
async def test_peer_error_racing_the_deadline_emits_one_envelope_and_one_done(
    monkeypatch, fast_deadline
) -> None:
    """Vector 3: the peer's job_error lands on the buffer while the generator is
    parked at the first-token yield and the mid-stream deadline has already
    expired.

    Both terminal signals are live at once. The wire must carry exactly one
    error envelope, exactly one DONE and no meta; the row must be finalized
    exactly once.
    """
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []
    job_id: uuid.UUID | None = None

    monkeypatch.setattr(jobs_router, "JOB_TIMEOUT_S", 0.1)

    try:
        async with factory() as db:
            pid, _ws = await _mk_peer(
                db, contributor, behavior=_stream_one_token_and_hold
            )
            peer_ids.append(pid)
            req = JobRequest(prompt="error races the deadline", model_slug=_MODEL)
            response = await jobs_router.dispatch_job_stream(req, db, requester)

        agen = response.body_iterator
        first = await agen.asend(None)
        assert "first" in first

        async with factory() as db:
            job_id = (await _all_job_ids_for_peers(db, peer_ids))[0]
        buf = await get_job(job_id)
        assert buf is not None

        # The mid-stream deadline expires while the generator is suspended...
        await asyncio.sleep(0.25)
        # ...and the peer's job_error lands one tick later, before the
        # generator is resumed into the deadline branch.
        buf.error_category = "inference_failed"
        buf.error_message = "peer died after the first token"

        rest = [chunk async for chunk in agen]
        body = first + "".join(rest)

        envelopes = _envelopes([first, *rest])
        errors = [e for e in envelopes if "error" in e]
        metas = [e for e in envelopes if "meta" in e]
        assert len(errors) == 1, f"expected exactly one error envelope, got {errors}"
        assert metas == [], "an error envelope must never be followed by meta"
        assert body.count("[DONE]") == 1, "exactly one DONE terminates the stream"

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == 1, "one attempt, one row, one finalize"
            db_job = await db.get(Job, job_id)
            assert db_job.status == "failed"
            assert db_job.terminal_reason in {"timeout", "peer_error"}, (
                "a job that both timed out and reported an error must settle "
                "on exactly one of the two failure outcomes"
            )
            assert db_job.error_category is not None
            assert db_job.output_tokens is None
            assert await _credit_rows(db, job_ids) == 0
            assert await _dispatched_count(db, peer_ids) == 0
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


# ===========================================================================
# Vector 4. Conflicting double finalize.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first,second",
    [
        (Outcome.COMPLETED, Outcome.CANCELLED),
        (Outcome.CANCELLED, Outcome.COMPLETED),
    ],
    ids=["completed_then_cancelled", "cancelled_then_completed"],
)
async def test_characterizes_conflicting_double_finalize(first, second) -> None:
    """Vector 4, now locked: `_finalize_job_in_db` is idempotent for a
    REPEATED outcome (the implementer's matrix proves that) and REFUSES a
    conflicting one.

    A row that has reached a terminal status (completed or failed) does not
    move again. The second, conflicting finalize is ignored and logged; the
    row and the ledger stay consistent with each other because neither is
    touched.

    completed -> cancelled: the row stays completed/completed with its
    output_tokens and its two minted ledger rows. The ledger said it was paid
    and the row still agrees.

    cancelled -> completed: the settled failure is NOT promoted back, and the
    late COMPLETED mints nothing.

    This is what makes a second finalize site (the settle-on-disconnect
    background task added for the never-started body, a future reaper, a
    drain-time settle) safe to add: the first terminal write wins.
    """
    await init_db()
    factory = get_session_factory()
    from coordinator.models.peer import Peer
    from sqlalchemy import delete

    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None

    try:
        async with factory() as db:
            peer = Peer(
                member_id=contributor, public_key=uuid.uuid4().hex, status="active"
            )
            db.add(peer)
            await db.flush()
            peer_id = peer.id
            job = Job(
                member_id=requester,
                peer_id=peer.id,
                model_slug=_MODEL,
                privacy_mode="pseudonymous",
                status="dispatched",
            )
            db.add(job)
            await db.flush()
            job_id = job.id
            await db.commit()

        await _finalize_job_in_db(
            job_id=job_id,
            terminal=first,
            error_category=None if first is Outcome.COMPLETED else "client_disconnected",
            error_message=None,
            output_tokens=100 if first is Outcome.COMPLETED else 0,
            duration_ms=1234 if first is Outcome.COMPLETED else 0,
        )
        await _finalize_job_in_db(
            job_id=job_id,
            terminal=second,
            error_category=None if second is Outcome.COMPLETED else "client_disconnected",
            error_message=None,
            output_tokens=100 if second is Outcome.COMPLETED else 0,
            duration_ms=1234 if second is Outcome.COMPLETED else 0,
        )

        async with factory() as db:
            db_job = await db.get(Job, job_id)
            ledger = await _credit_rows(db, [job_id])
            assert db_job.terminal_reason == first.value, (
                "the FIRST terminal outcome wins: a settled row does not move"
            )
            if first is Outcome.COMPLETED:
                assert db_job.status == "completed"
                assert db_job.output_tokens == 100, (
                    "the completed pass wrote the billable totals and the "
                    "ignored cancel does not disturb them"
                )
                assert ledger == 2, (
                    "the row still says completed, so the minted rows still "
                    "match it"
                )
            else:
                assert db_job.status == "failed"
                assert db_job.output_tokens is None, (
                    "a job settled as cancelled has no billable output, and "
                    "the ignored completed pass must not add one"
                )
                assert ledger == 0, (
                    "a job already settled as cancelled must never be promoted "
                    "back to completed and minted"
                )
    finally:
        async with factory() as cleanup_db:
            async with cleanup_db.begin():
                if job_id is not None:
                    await cleanup_db.execute(
                        delete(CreditLedger).where(CreditLedger.job_id == job_id)
                    )
                    await cleanup_db.execute(delete(Job).where(Job.id == job_id))
                if peer_id is not None:
                    await cleanup_db.execute(delete(Peer).where(Peer.id == peer_id))
        await close_db()


# ===========================================================================
# Vector 5. Dispatch failures across the whole retry budget.
# ===========================================================================


@pytest.mark.asyncio
async def test_send_fails_twice_then_succeeds(fast_deadline) -> None:
    """Vector 5a: sends fail on attempts 1 and 2 and succeed on 3. Three Job
    rows, two dispatch_failed, one completed, the client gets a 200, and only
    the served row mints."""
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    try:
        async with factory() as db:
            # Equal scores tie-break to the most recently registered peer, so
            # register in reverse of the intended pick order.
            good_id, good_ws = await _mk_peer(
                db, contributor, behavior=_stream_and_complete
            )
            broken_b = _BrokenWS()
            broken_b_id = await _mk_peer_with_ws(db, contributor, broken_b)
            broken_a = _BrokenWS()
            broken_a_id = await _mk_peer_with_ws(db, contributor, broken_a)
            peer_ids = [broken_a_id, broken_b_id, good_id]

        app = _make_app(factory, requester)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/jobs/stream",
                json={"prompt": "two dead sockets", "model_slug": _MODEL},
            )

        assert response.status_code == 200
        assert "hello" in response.text
        assert len(broken_a.sent) == 1 and len(broken_b.sent) == 1
        assert len(good_ws.sent) == 1

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == 3, "one Job row per dispatch attempt"
            rows = (
                await db.execute(select(Job).where(Job.id.in_(job_ids)))
            ).scalars().all()
            reasons = sorted(r.terminal_reason for r in rows)
            assert reasons == ["completed", "dispatch_failed", "dispatch_failed"]
            for r in rows:
                if r.terminal_reason == "dispatch_failed":
                    assert r.status == "failed"
                    assert r.error_category == "dispatch_failed"
                    assert r.output_tokens is None
            assert await _dispatched_count(db, peer_ids) == 0
            served = [r for r in rows if r.terminal_reason == "completed"][0]
            assert await _credit_rows(db, [served.id]) == 2
            failed_ids = [r.id for r in rows if r.terminal_reason != "completed"]
            assert await _credit_rows(db, failed_ids) == 0
    finally:
        await _drain_background_tasks()
        job_ids_cleanup: list[uuid.UUID] = []
        try:
            async with get_session_factory()() as db:
                job_ids_cleanup = await _all_job_ids_for_peers(db, peer_ids)
        except Exception:
            pass
        await _cleanup(peer_ids, job_ids_cleanup)


@pytest.mark.asyncio
async def test_send_fails_on_every_attempt_returns_503(fast_deadline) -> None:
    """Vector 5b: every socket is dead. 503, three failed rows, nothing left at
    'dispatched', no credit rows, and no in-flight buffer leaked."""
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []
    sockets: list[_BrokenWS] = []

    try:
        async with factory() as db:
            for _ in range(jobs_router.MAX_DISPATCH_ATTEMPTS):
                ws = _BrokenWS()
                pid = await _mk_peer_with_ws(db, contributor, ws)
                sockets.append(ws)
                peer_ids.append(pid)

        app = _make_app(factory, requester)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/jobs/stream",
                json={"prompt": "every socket is dead", "model_slug": _MODEL},
            )

        assert response.status_code == 503
        assert all(len(ws.sent) == 1 for ws in sockets), (
            "each peer must be tried exactly once, never re-picked"
        )

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == jobs_router.MAX_DISPATCH_ATTEMPTS
            rows = (
                await db.execute(select(Job).where(Job.id.in_(job_ids)))
            ).scalars().all()
            for r in rows:
                assert r.status == "failed"
                assert r.terminal_reason == "dispatch_failed"
                assert r.error_category == "dispatch_failed"
                assert r.completed_at is not None
            assert await _dispatched_count(db, peer_ids) == 0
            assert await _credit_rows(db, job_ids) == 0
            for jid in job_ids:
                assert await get_job(jid) is None, (
                    "a failed send must release the in-flight registry slot"
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


# ===========================================================================
# Vector 6. Synchronous POST /jobs.
# ===========================================================================


@pytest.mark.asyncio
async def test_sync_completion_settles_the_row_and_mints_nothing() -> None:
    """Vector 6: the sync success branch writes the row inline rather than
    through the finalizer. terminal_reason must still be recorded, and the
    deliberate no-mint policy must hold."""
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    try:
        async with factory() as db:
            pid, _ws = await _mk_peer(db, contributor, behavior=_stream_and_complete)
            peer_ids.append(pid)

        app = _make_app(factory, requester)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/jobs", json={"prompt": "sync happy path", "model_slug": _MODEL}
            )

        assert response.status_code == 200
        assert response.json()["response"] == "hello"

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            db_job = await db.get(Job, job_ids[0])
            assert db_job.status == "completed"
            assert db_job.terminal_reason == "completed"
            assert db_job.error_category is None
            assert db_job.output_tokens == 12
            assert await _dispatched_count(db, peer_ids) == 0
            assert await _credit_rows(db, job_ids) == 0, (
                "POST /jobs has never minted; routing it through the finalizer "
                "would have started"
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


@pytest.mark.asyncio
async def test_sync_peer_error_settles_the_row_as_peer_error() -> None:
    """Vector 6: a peer-reported job_error on the sync path must leave
    failed/peer_error with the peer's category preserved, return 502, and leave
    nothing at 'dispatched'."""
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    async def _blow_up(buf) -> None:  # noqa: ANN001
        buf.error_category = "inference_failed"
        buf.error_message = "model failed to load"
        await complete_job(buf.job_id)

    try:
        async with factory() as db:
            pid, _ws = await _mk_peer(db, contributor, behavior=_blow_up)
            peer_ids.append(pid)

        app = _make_app(factory, requester)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/jobs", json={"prompt": "sync error path", "model_slug": _MODEL}
            )

        assert response.status_code == 502
        assert response.json()["detail"]["error_category"] == "inference_failed"

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            db_job = await db.get(Job, job_ids[0])
            assert db_job.status == "failed"
            assert db_job.terminal_reason == "peer_error"
            assert db_job.error_category == "inference_failed"
            assert db_job.output_tokens is None
            assert await _dispatched_count(db, peer_ids) == 0
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


@pytest.mark.asyncio
async def test_sync_dead_socket_returns_502_and_settles_dispatch_failed(
    fast_deadline,
) -> None:
    """Vector 6: PeerDispatchFailed subclasses HTTPException(502), and the sync
    endpoint does not retry -- so the row must already be settled as
    dispatch_failed by the time the 502 unwinds past the handler."""
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []

    try:
        async with factory() as db:
            ws = _BrokenWS()
            pid = await _mk_peer_with_ws(db, contributor, ws)
            peer_ids.append(pid)

        app = _make_app(factory, requester)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/jobs", json={"prompt": "dead socket, sync", "model_slug": _MODEL}
            )

        assert response.status_code == 502

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            assert len(job_ids) == 1
            db_job = await db.get(Job, job_ids[0])
            assert db_job.status == "failed"
            assert db_job.terminal_reason == "dispatch_failed"
            assert db_job.error_category == "dispatch_failed"
            assert await _dispatched_count(db, peer_ids) == 0
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


# ===========================================================================
# Vector 7. Abusive inputs to the derived first-token deadline.
# ===========================================================================


@pytest.mark.asyncio
async def test_derived_deadline_clamp_holds_under_abuse() -> None:
    """Vector 7: a 5 MB prompt, 4 MB of chat history, a tokens_per_second of 0 /
    negative / NaN, a peer with no capability row and an empty
    hardware_summary, and a PeerConnection whose Peer row does not exist at
    all. Nothing may raise and every result must land inside
    [FIRST_TOKEN_DEADLINE_S, FIRST_TOKEN_MAX_S]."""
    from coordinator.routers.jobs import (
        FIRST_TOKEN_DEADLINE_S,
        FIRST_TOKEN_MAX_S,
        _first_token_deadline,
    )

    await init_db()
    peer_ids: list[uuid.UUID] = []
    try:
        async with get_session_factory()() as db:
            bare_id, bare_conn = await _mk_deadline_peer(
                db, hardware={}, with_capability=False
            )
            zero_id, zero_conn = await _mk_deadline_peer(
                db, hardware={}, tokens_per_second=0.0
            )
            neg_id, neg_conn = await _mk_deadline_peer(
                db, hardware={}, tokens_per_second=-42.0
            )
            nan_id, nan_conn = await _mk_deadline_peer(
                db, hardware={}, tokens_per_second=float("nan")
            )
            tiny_id, tiny_conn = await _mk_deadline_peer(
                db, hardware={}, tokens_per_second=1e-300
            )
            peer_ids += [bare_id, zero_id, neg_id, nan_id, tiny_id]

            # A connection whose Peer row was deleted between selection and
            # here: the outer join returns no row at all.
            ghost_conn = PeerConnection(
                peer_id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                ws=None,
                member_id=uuid.uuid4(),
            )

            huge_prompt = JobRequest(prompt="x" * 5_000_000, model_slug=_DEADLINE_MODEL)
            huge_history = JobRequest(
                prompt="",
                messages=[
                    ChatMessage(role="user", content="y" * 2_000_000),
                    ChatMessage(role="assistant", content="z" * 2_000_000),
                ],
                model_slug=_DEADLINE_MODEL,
            )
            tiny_prompt = JobRequest(prompt="hi", model_slug=_DEADLINE_MODEL)

            cases = {
                "5MB prompt / no capability row / empty hardware": (
                    huge_prompt,
                    bare_conn,
                ),
                "4MB history / no capability row": (huge_history, bare_conn),
                "5MB prompt / tps=0": (huge_prompt, zero_conn),
                "5MB prompt / tps<0": (huge_prompt, neg_conn),
                "5MB prompt / tps=NaN": (huge_prompt, nan_conn),
                "5MB prompt / tps=1e-300": (huge_prompt, tiny_conn),
                "short prompt / tps=NaN": (tiny_prompt, nan_conn),
                "short prompt / no Peer row": (tiny_prompt, ghost_conn),
                "5MB prompt / no Peer row": (huge_prompt, ghost_conn),
            }
            results: dict[str, float] = {}
            for label, (req, conn) in cases.items():
                results[label] = await _first_token_deadline(db, req, conn)

        for label, value in results.items():
            assert value == value, f"{label}: deadline is NaN"
            assert FIRST_TOKEN_DEADLINE_S <= value <= FIRST_TOKEN_MAX_S, (
                f"{label}: deadline {value} escaped the clamp"
            )
        for label in (
            "5MB prompt / no capability row / empty hardware",
            "4MB history / no capability row",
            "5MB prompt / tps=0",
            "5MB prompt / tps<0",
            "5MB prompt / tps=NaN",
            "5MB prompt / tps=1e-300",
            "5MB prompt / no Peer row",
        ):
            assert results[label] == FIRST_TOKEN_MAX_S, (
                f"{label}: a multi-megabyte prompt must saturate the cap, not "
                f"fall back to the floor ({results[label]})"
            )
        # A 2-char prompt is 0.5 estimated tokens; the CPU tier proxy is
        # 1.0 * 25 tok/s, so the derived budget is floor + 0.02. Landing on
        # EXACTLY the floor would be the tell that a NaN propagated all the way
        # through and was swallowed by `max(floor, nan)` -- which returns the
        # floor rather than raising, so the clamp hides the bad input instead
        # of rejecting it.
        for label in ("short prompt / tps=NaN", "short prompt / no Peer row"):
            assert results[label] == pytest.approx(
                FIRST_TOKEN_DEADLINE_S + 0.02, abs=0.005
            ), (
                f"{label}: a NaN or missing throughput must degrade to the "
                f"hardware-tier proxy, not to a NaN budget"
            )
    finally:
        await _cleanup_deadline_peers(peer_ids)


# ===========================================================================
# Vector 10. Persistence of terminal_reason / error_category everywhere.
# ===========================================================================


@pytest.mark.asyncio
async def test_every_settled_row_records_a_terminal_reason() -> None:
    """Vector 10: the migration's null semantics say terminal_reason IS NULL
    means 'predates the contract'. Nothing written by the current code may land
    in that bucket, and the string must fit the 32-char column."""
    await init_db()
    factory = get_session_factory()
    from coordinator.models.peer import Peer
    from sqlalchemy import delete

    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_id: uuid.UUID | None = None
    job_ids: list[uuid.UUID] = []

    try:
        async with factory() as db:
            peer = Peer(
                member_id=contributor, public_key=uuid.uuid4().hex, status="active"
            )
            db.add(peer)
            await db.flush()
            peer_id = peer.id
            for _ in Outcome:
                job = Job(
                    member_id=requester,
                    peer_id=peer.id,
                    model_slug=_MODEL,
                    privacy_mode="pseudonymous",
                    status="dispatched",
                )
                db.add(job)
                await db.flush()
                job_ids.append(job.id)
            await db.commit()

        for outcome, jid in zip(Outcome, job_ids):
            await _finalize_job_in_db(
                job_id=jid,
                terminal=outcome,
                error_category=None if outcome is Outcome.COMPLETED else outcome.value,
                error_message=None,
                output_tokens=5,
                duration_ms=6,
            )

        async with factory() as db:
            for outcome, jid in zip(Outcome, job_ids):
                db_job = await db.get(Job, jid)
                assert db_job.terminal_reason == outcome.value
                assert len(db_job.terminal_reason) <= 32, (
                    "terminal_reason is String(32) in migration 0011"
                )
                assert db_job.status in {"completed", "failed"}
                assert db_job.status != "dispatched"
                if outcome is not Outcome.COMPLETED:
                    assert len(db_job.error_category) <= 64
    finally:
        async with factory() as cleanup_db:
            async with cleanup_db.begin():
                if job_ids:
                    await cleanup_db.execute(
                        delete(CreditLedger).where(CreditLedger.job_id.in_(job_ids))
                    )
                    await cleanup_db.execute(delete(Job).where(Job.id.in_(job_ids)))
                if peer_id is not None:
                    await cleanup_db.execute(delete(Peer).where(Peer.id == peer_id))
        await close_db()


# ===========================================================================
# Acceptance criterion: a stalled peer can never reach the success accounting
# branch.
# ===========================================================================


@pytest.mark.asyncio
async def test_a_stalled_peer_never_reaches_the_success_branch(
    monkeypatch, fast_deadline
) -> None:
    """The criterion the whole change is measured against, stated directly: a
    peer that produces a first token and then stalls forever must leave a
    failed/timed-out row, no meta envelope, and no ledger rows -- no matter how
    plausible the totals it reported before stalling."""
    await init_db()
    factory = get_session_factory()
    requester = uuid.uuid4()
    contributor = uuid.uuid4()
    peer_ids: list[uuid.UUID] = []
    job_id: uuid.UUID | None = None

    monkeypatch.setattr(jobs_router, "JOB_TIMEOUT_S", 0.2)

    async def _stall_with_flattering_totals(buf) -> None:  # noqa: ANN001
        buf.tokens.append("t")
        buf.output_tokens = 4096  # the peer claims a big, billable job
        buf.duration_ms = 1
        buf.finish_reason = "stop"
        buf.token_stream.put_nowait("t")

    try:
        async with factory() as db:
            pid, _ws = await _mk_peer(
                db, contributor, behavior=_stall_with_flattering_totals
            )
            peer_ids.append(pid)

        app = _make_app(factory, requester)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/jobs/stream",
                json={"prompt": "stall forever", "model_slug": _MODEL},
            )

        assert response.status_code == 200
        assert '"category": "timeout"' in response.text
        assert '"meta"' not in response.text

        await _drain_background_tasks()

        async with factory() as db:
            job_ids = await _all_job_ids_for_peers(db, peer_ids)
            job_id = job_ids[0]
            db_job = await db.get(Job, job_id)
            assert db_job.status == "failed"
            assert db_job.terminal_reason == "timeout"
            assert db_job.output_tokens is None, (
                "a stalled peer's self-reported 4096 tokens must never be "
                "written to the row"
            )
            assert await _credit_rows(db, job_ids) == 0
            assert await _dispatched_count(db, peer_ids) == 0
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
