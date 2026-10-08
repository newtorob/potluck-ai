"""Job dispatch endpoints.

Two consumer paths:
- POST /jobs        — synchronous; returns full collected response when
                      inference completes. Used by curl smoke + tests.
- POST /jobs/stream — Server-Sent Events; emits OpenAI-compatible delta
                      chunks (`data: {"choices":[{"delta":{"content":"…"}}]}`)
                      as tokens arrive. Used by the desktop chat composer
                      when the user toggles "Use contributor network."

The SSE shape mirrors the local sidecar's /chat endpoint so the frontend
chat-stream parser is reused without modification.

Privacy invariant (memory 42a7e671):
    JobAssignedMessage MUST NOT carry member_id, conversation_id, or any
    other identifier that links the prompt back to its requester. The
    schema for JobAssignedMessage doesn't even have a member_id field —
    that's the architectural enforcement. Any future change that adds an
    identifying field to JobAssignedMessage is a manifesto-level bug.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal, Self

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.db.database import get_db, get_session_factory
from coordinator.middleware.auth import require_coordinator_token
from coordinator.middleware.session_auth import resolve_session_member_id
from coordinator.models.job import Job
from coordinator.schemas.peer import (
    MAX_OUTPUT_TOKENS,
    ChatMessage,
    JobAssignedMessage,
    SamplingParams,
)
from coordinator.services.peer_registry import (
    JobBuffer,
    PeerConnection,
    complete_job,
    register_job,
    select_peer_for_request,
)

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["jobs"])

# Module-level strong reference to background tasks so CPython's GC (and the
# Python 3.12 RuntimeWarning) never discards a pending Task before it completes.
# Tasks self-remove via a done-callback; the set only holds running tasks.
_background_tasks: set[asyncio.Task] = set()

# Per-job hard ceiling. Even a 512-token completion at 5 tok/s on a slow
# peer should finish well under this. If we hit it, the peer hung and we
# should stop blocking the requesting client.
JOB_TIMEOUT_S = 120.0

# ---- Active first-token failover (streaming endpoint only) ---------------
#
# How long we'll wait, per dispatch attempt, for a peer to either produce
# its FIRST token or signal completion (JobCompleteMessage with zero
# tokens is a valid empty completion, not a failure). A peer that clears
# neither bar within this window is presumed dead-on-arrival (hung,
# black-holed, crashed mid-load) and we fail over to the next-best peer
# BEFORE any bytes have reached the client -- see `_dispatch_stream_with_failover`.
# This is a FLOOR, not the whole budget: see `_first_token_deadline`, which
# adds an estimate of this request's prompt-processing time on this peer.
FIRST_TOKEN_DEADLINE_S = 5.0

# Upper bound on the derived first-token deadline. A peer that has produced
# nothing after this long is dead by any reading a requester would accept.
FIRST_TOKEN_MAX_S = 90.0

# Prompt-processing throughput implied by one unit of hardware-tier score,
# used only when no measured PeerCapability.tokens_per_second exists for
# this (peer, model). Deliberately coarse: it exists to stop a long prompt
# from tripping the flat floor, not to predict latency.
_TIER_PP_TOKENS_PER_SECOND = 25.0

# The usual English rule of thumb for character-to-token ratio. Only the
# order of magnitude matters for a deadline.
_CHARS_PER_TOKEN = 4.0

# Hard ceiling on how many peers we'll try for a single streaming request
# before giving up and returning 503. Bounds worst-case PRE-STREAM latency to
# roughly MAX_DISPATCH_ATTEMPTS * the DERIVED per-attempt deadline (see
# `_first_token_deadline`), i.e. 3 * 5s for a short prompt and 3 * 90s in the
# pathological case of a very long prompt on a very slow peer. Nothing has
# reached the client at that point, so the cost is latency, not a broken
# stream.
MAX_DISPATCH_ATTEMPTS = 3


class Outcome(StrEnum):
    """The single terminal state of one dispatch attempt.

    Exactly one Outcome per Job row, decided by whoever owns the attempt when
    it stops: the stream generator, the synchronous endpoint, or the dispatch
    preamble. `_finalize_job_in_db` is the only place it is applied, and it
    maps the outcome to `jobs.status` + `jobs.terminal_reason` plus the credit
    policy:

        COMPLETED       -> status "completed"; output_tokens + duration_ms are
                           written; credits mint when the job is cross-member
                           and produced tokens.
        TIMEOUT         -> status "failed"; nothing minted.
        CANCELLED       -> status "failed"; nothing minted.
        PEER_ERROR      -> status "failed"; nothing minted.
        DISPATCH_FAILED -> status "failed"; nothing minted.

    No outcome writes a reputation or quality signal. That is deliberate:
    "peer_disconnected" is a coordinator-synthesized sentinel and a dropped
    connection is not evidence of peer fault. See the reputation note on
    services/peer_registry.py::drain_peer_jobs before adding one.

    The `jobs.status` vocabulary stays {dispatched, completed, failed} so the
    aggregate queries that filter on it (routers/me.py, routers/transparency.py,
    my_job_stats below) keep reporting the same numbers. Granularity lives in
    `terminal_reason` (migration 0011).
    """

    COMPLETED = "completed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PEER_ERROR = "peer_error"
    DISPATCH_FAILED = "dispatch_failed"


# The `jobs.status` values that mean "settled". A row in one of these does not
# move again: see the terminal-state lock in `_finalize_job_in_db`.
_TERMINAL_STATUSES = frozenset({"completed", "failed"})


class PeerDispatchFailed(HTTPException):
    """The JobAssignedMessage could not be handed to the peer's socket.

    Subclasses HTTPException with status 502 so the synchronous POST /jobs
    path returns exactly what it returns today when a peer's socket is dead.
    The streaming path catches it instead, excludes that peer, and spends the
    next dispatch attempt: a socket that died between selection and send must
    not reach the client as a 502 while retries remain.

    The Job row for the failed attempt is ALREADY finalized (DISPATCH_FAILED)
    before this is raised, so no handler has to settle it.
    """

    def __init__(
        self, *, peer_id: uuid.UUID, job_id: uuid.UUID, detail: str
    ) -> None:
        super().__init__(status_code=502, detail=detail)
        self.peer_id = peer_id
        self.job_id = job_id


# Widest routing tier the requester will accept. The coordinator always tries
# the closest/most-trusted tier first and widens outward only up to this limit.
# "local" is never a coordinator scope — a local-served prompt never produces a
# coordinator job at all.
RoutingScope = Literal["mine", "circle", "pool"]


class JobRequest(BaseModel):
    prompt: str
    # Optional multi-turn history. When present, the peer uses messages as
    # conversation context instead of (or in addition to) prompt. The
    # coordinator relays whatever the sidecar sends — policy (circle always
    # sends messages; pool sends messages only on opt-in) is enforced on the
    # sidecar side. ChatMessage carries role + content only; no requester
    # identity ever crosses to the peer.
    messages: list[ChatMessage] | None = None
    model_slug: str = "stub-echo"  # m3 stub default; real models when llama-cpp lands
    sampling: SamplingParams = Field(default_factory=SamplingParams)
    # "mine"   -> only machines this member owns (503 if none online)
    # "circle" -> only machines whose member is in the named circle (503 if none online)
    # "pool"   -> mine, then the open pool (default; backward-compatible)
    scope: RoutingScope = "pool"
    # Required when scope="circle". Must be a circle owned by the caller.
    # Omit (or pass null) for all other scopes; conflicting intent is rejected.
    circle_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_circle_scope(self) -> Self:
        if self.scope == "circle" and self.circle_id is None:
            raise ValueError("circle_id is required when scope='circle'.")
        if self.circle_id is not None and self.scope != "circle":
            raise ValueError("circle_id requires scope='circle'.")
        return self


class JobResponse(BaseModel):
    job_id: uuid.UUID
    response: str
    output_tokens: int
    duration_ms: int
    finish_reason: str
    # Which tier actually served the job ("mine" / "pool"). A tier label only —
    # never a peer_id. peer_id is intentionally omitted from the response:
    # exposing it would let a requester shop for a specific peer, which breaks
    # the anonymity guarantee the manifesto promises.
    served_scope: RoutingScope


class JobErrorResponse(BaseModel):
    job_id: uuid.UUID
    error_category: str
    error_message: str


class _StreamStart:
    """One-bit handshake between a response body generator and the
    settle-on-disconnect hook attached to the same response.

    `started` flips to True as the generator's very first statement, before
    any await or yield, and answers exactly one question: did the generator
    body ever run? If it did, its `finally:` owns settlement and the hook must
    keep its hands off. If it never ran, nothing else in the process will ever
    settle that job. `settled` makes the hook itself safe to call twice.
    """

    __slots__ = ("started", "settled")

    def __init__(self) -> None:
        self.started = False
        self.settled = False


class _GuardedStream:
    """Async-iterable wrapper that guarantees the settle hook runs even when
    the response body is closed without ever being iterated.

    Two doors lead out of a stream that never started, and the hook has to
    cover both:

      * ASGI. Starlette cancels `stream_response` before its first
        `async for` when the client is already gone. That door is covered by
        the response's BackgroundTask, which starlette awaits after its task
        group exits on BOTH normal completion and disconnect.
      * A caller that closes `response.body_iterator` directly. Closing an
        async generator that has never started runs none of its body, so no
        `finally:` fires. That door is covered by `aclose` below.

    Everything else is straight delegation, so the object behaves like the
    generator it wraps (`asend` / `athrow` / `aclose` / `async for`).
    """

    __slots__ = ("_agen", "_settle")

    def __init__(
        self,
        agen: AsyncGenerator[str, None],
        settle: "Callable[[], Awaitable[None]]",
    ) -> None:
        self._agen = agen
        self._settle = settle

    def __aiter__(self) -> "_GuardedStream":
        return self

    async def __anext__(self) -> str:
        return await self._agen.__anext__()

    async def asend(self, value: None) -> str:
        return await self._agen.asend(value)

    async def athrow(self, *args: object, **kwargs: object) -> str:
        return await self._agen.athrow(*args, **kwargs)  # type: ignore[arg-type]

    async def aclose(self) -> None:
        await self._agen.aclose()
        await self._settle()


async def _dispatch_to_peer(
    db: AsyncSession,
    req: JobRequest,
    member_id: uuid.UUID,
    exclude_peer_ids: set[uuid.UUID] | None = None,
) -> tuple[PeerConnection, Job, JobBuffer, RoutingScope]:
    """Shared dispatch preamble for the sync + streaming job endpoints.

    Picks an eligible peer (503 if none), creates the Job row, registers the
    in-memory buffer, and delivers the JobAssignedMessage (502 if the peer is
    unreachable). Returns (peer_conn, job, buf); the caller awaits buf and logs
    its own "dispatched" event.

    `exclude_peer_ids`, when given, is forwarded to `select_peer_for_request`
    so a peer that already proved dead-on-arrival on an earlier attempt of
    THIS request (see `_dispatch_stream_with_failover`) is never re-picked.
    Default None preserves current behavior for every existing caller
    (including the sync `dispatch_job` endpoint, which does not retry).

    CRITICAL PRIVACY POINT: JobAssignedMessage carries ONLY job_id + model_slug
    + prompt + sampling. Adding member_id or any identifying field here is a
    blinded-routing violation (the schema has no member_id field, so it's also
    a type error, but the rule deserves the comment).
    """
    # Circle scope: the caller must be in the circle; candidates are the
    # machines of its other members.
    circle_lenders: set[uuid.UUID] | None = None
    if req.scope == "circle":
        if req.circle_id is None:
            raise HTTPException(
                status_code=422,
                detail="circle_id is required when scope='circle'.",
            )
        from coordinator.services.circles import lender_member_ids, membership_of

        # Not being in the circle and the circle not existing look the same
        # to the caller, so a circle ID cannot be probed.
        if await membership_of(db, req.circle_id, member_id) is None:
            raise HTTPException(
                status_code=404,
                detail="circle not found",
            )
        circle_lenders = await lender_member_ids(db, req.circle_id, member_id)

    peer_conn, served_scope = await select_peer_for_request(
        db,
        model_slug=req.model_slug,
        requester_member_id=member_id,
        scope=req.scope,
        circle_member_ids=circle_lenders,
        exclude_peer_ids=exclude_peer_ids,
    )
    if peer_conn is None:
        # Distinguish the scope-specific failure modes so the caller knows
        # whether to widen scope, wait, or pick a different slug.
        from coordinator.services.peer_registry import online_peer_count

        online = await online_peer_count()
        if req.scope == "mine":
            detail = (
                f"No machine you own is online and serving '{req.model_slug}'. "
                f"Bring one of your machines online, or use scope=pool to allow the open pool."
            )
        elif req.scope == "circle":
            detail = (
                f"No machine in this circle is online for '{req.model_slug}'. "
                f"Ask circle members to bring a machine online, or use scope=pool."
            )
        elif online == 0:
            detail = "No contributor peers connected. Bring at least one online and retry."
        else:
            detail = (
                f"No online peer serves model '{req.model_slug}'. "
                f"{online} peer(s) online but none advertise that capability. "
                f"Either pick a different model_slug or wait for an eligible peer to connect."
            )
        raise HTTPException(status_code=503, detail=detail)

    job = Job(
        member_id=member_id,
        peer_id=peer_conn.peer_id,
        model_slug=req.model_slug,
        privacy_mode="pseudonymous",
        status="dispatched",
    )
    db.add(job)
    await db.flush()  # populates job.id
    await db.commit()

    buf = await register_job(
        job.id, max_tokens=req.sampling.max_tokens, peer_id=peer_conn.peer_id
    )
    assigned = JobAssignedMessage(
        job_id=job.id,
        model_slug=req.model_slug,
        prompt=req.prompt,
        messages=req.messages,
        sampling=req.sampling,
    )
    try:
        await peer_conn.ws.send_json(assigned.model_dump(mode="json"))
    except Exception as exc:
        logger.warning(
            "job_dispatch_send_failed",
            job_id=str(job.id),
            peer_id=str(peer_conn.peer_id),
            reason=str(exc),
        )
        await complete_job(job.id)
        # Settle the row right here. Without this the attempt stayed at
        # 'dispatched' forever: the row was already committed above, and the
        # 502 below unwound past every caller that could have finalized it.
        await _finalize_job_in_db(
            job_id=job.id,
            terminal=Outcome.DISPATCH_FAILED,
            error_category="dispatch_failed",
            error_message=f"Couldn't deliver job to peer: {exc}",
            output_tokens=0,
            duration_ms=0,
        )
        raise PeerDispatchFailed(
            peer_id=peer_conn.peer_id,
            job_id=job.id,
            detail=f"Couldn't deliver job to peer: {exc}",
        ) from exc

    return peer_conn, job, buf, served_scope


@router.post(
    "",
    response_model=JobResponse,
    responses={502: {"model": JobErrorResponse}},
    dependencies=[Depends(require_coordinator_token)],
)
async def dispatch_job(
    req: JobRequest,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> JobResponse:
    """Dispatch one inference job to an online peer and return the full
    response when it completes.

    Synchronous: this endpoint blocks until the peer finishes (or
    JOB_TIMEOUT_S elapses). Good enough for the m3 stub smoke test.
    Streaming endpoints come in a follow-up.
    """
    # ---- 1-3. Pick peer, create job, deliver (shared with the stream endpoint) ---- #
    peer_conn, job, buf, served_scope = await _dispatch_to_peer(db, req, member_id)
    job_id = job.id
    logger.info(
        "job_dispatched",
        job_id=str(job_id),
        # Note: NO member_id in this log line. Coordinator-side logs are
        # internal, but we keep peer-bound observability consistent with
        # what crosses the wire — easier to reason about + safer if logs
        # ever ship somewhere we don't fully control.
        peer_id=str(peer_conn.peer_id),
        model_slug=req.model_slug,
        prompt_chars=len(req.prompt),
    )

    # ---- 4. Await completion ---- #
    try:
        await asyncio.wait_for(buf.done.wait(), timeout=JOB_TIMEOUT_S)
    except asyncio.TimeoutError:
        await complete_job(job_id)
        logger.warning("job_timeout", job_id=str(job_id), peer_id=str(peer_conn.peer_id))
        # Settle the row BEFORE raising. Raising straight through left the
        # job at 'dispatched' forever, which no later code path ever cleaned
        # up (there is no reaper for job rows, only for peers).
        await _finalize_job_in_db(
            job_id=job_id,
            terminal=Outcome.TIMEOUT,
            error_category="timeout",
            error_message=f"peer did not complete within {JOB_TIMEOUT_S}s",
            output_tokens=0,
            duration_ms=0,
        )
        raise HTTPException(
            status_code=504,
            detail=f"Peer didn't complete within {JOB_TIMEOUT_S}s",
        )

    # ---- 5. Return result OR error ---- #
    if buf.error_category is not None:
        # Peer sent JobErrorMessage. Settle the row through the one
        # finalizer (which never mints for a non-COMPLETED outcome) and
        # return 502.
        await _finalize_job_in_db(
            job_id=job_id,
            terminal=Outcome.PEER_ERROR,
            error_category=buf.error_category,
            error_message=buf.error_message,
            output_tokens=0,
            duration_ms=0,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "job_id": str(job_id),
                "error_category": buf.error_category,
                "error_message": buf.error_message or "(no message)",
            },
        )

    response_text = "".join(buf.tokens)

    # Success. Written inline on the request session rather than through
    # `_finalize_job_in_db` ON PURPOSE: this endpoint has never minted
    # credits, and routing it through the finalizer would start. The row
    # still records the same terminal outcome the streaming path records.
    job.status = "completed"
    job.terminal_reason = Outcome.COMPLETED.value
    job.error_category = None
    job.output_tokens = buf.output_tokens
    job.duration_ms = buf.duration_ms
    job.completed_at = datetime.now(timezone.utc)
    await db.commit()

    return JobResponse(
        job_id=job_id,
        response=response_text,
        output_tokens=buf.output_tokens,
        duration_ms=buf.duration_ms,
        finish_reason=buf.finish_reason,
        served_scope=served_scope,
    )


# ---- Streaming endpoint ------------------------------------------------- #


def _sse_chunk(content: str) -> str:
    """Encode one token as an OpenAI-compatible SSE delta line.

    Matches the shape the local sidecar's /chat/stream emits, so the
    frontend chat-stream parser is reused without modification:
        data: {"choices":[{"delta":{"content":"…"}}]}\n\n
    """
    payload = {"choices": [{"delta": {"content": content}}]}
    return f"data: {json.dumps(payload)}\n\n"


def _sse_done() -> str:
    return "data: [DONE]\n\n"


def _sse_meta(
    output_tokens: int, duration_ms: int, finish_reason: str, served_scope: str
) -> str:
    """Encode a job-completion summary as an SSE chunk.

    The frontend uses this to badge the response — `served_scope` ("mine" /
    "pool") lets it show "ran on your machine" vs "served by the network"
    without exposing peer identity. `served_by` + `served_scope` are tier
    labels only; peer_id, hostname, and any other identifying fields are
    deliberately absent per the blinded-routing invariant (memory 42a7e671).
    """
    payload = {
        "meta": {
            "served_by": "contributor",
            "served_scope": served_scope,
            "output_tokens": output_tokens,
            "duration_ms": duration_ms,
            "finish_reason": finish_reason,
        }
    }
    return f"data: {json.dumps(payload)}\n\n"


def _sse_error(category: str, message: str) -> str:
    """Encode a job_error as an SSE chunk in a shape the frontend can
    surface to the user. Uses an OpenAI-style error envelope rather than
    the delta envelope so the parser can distinguish."""
    payload = {"error": {"category": category, "message": message}}
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# User-feedback quality signal
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    rating: Literal["up", "down"]


@router.post(
    "/{job_id}/feedback",
    dependencies=[Depends(require_coordinator_token)],
)
async def job_feedback(
    job_id: uuid.UUID,
    req: FeedbackRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record a binary quality signal for a completed job.

    Maps the caller's thumbs-up / thumbs-down rating to a quality score and
    records it against the peer that served the job.

    PRIVACY: records only the binary rating plus the job's peer_id and
    model_slug.  Prompt and completion content are never read, stored, or
    forwarded here.

    Follow-on: frontend thumbs-down UI that calls this endpoint post-response.
    """
    job = await db.get(Job, job_id)
    if job is None or job.peer_id is None:
        raise HTTPException(status_code=404, detail="job not found")

    score = 1.0 if req.rating == "up" else 0.0
    from coordinator.services.peer_quality import record_quality_signal

    await record_quality_signal(
        db,
        peer_id=job.peer_id,
        model_slug=job.model_slug,
        signal_type="user_feedback",
        score=score,
        sample_size=1,
    )
    await db.commit()
    logger.info(
        "job_feedback_recorded",
        job_id=str(job_id),
        rating=req.rating,
        # peer_id intentionally omitted from log: coordinator-side internal
        # log but we keep peer-bound observability consistent with the wire.
    )
    return {"recorded": True}


@router.get("/stats")
async def my_job_stats(
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> dict:
    """Per-member requester stats for the desktop /network view.

    Filters by the caller's session member_id (the dev fixture STUB_MEMBER_ID
    until POTLUCK_SESSION_AUTH_ENABLED is set).

    Returns counts + token totals + a coarse latency band (p50). NO
    peer_id, NO per-job records — aggregate-only, in keeping with the
    blinded-routing rule. The frontend's "Your usage" card on /network
    consumes this directly.
    """
    from datetime import timedelta
    from sqlalchemy import func, select

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    base = select(Job).where(
        Job.member_id == member_id,
        Job.completed_at > day_ago,
        Job.status == "completed",
    )

    jobs_24h = await db.scalar(
        select(func.count()).select_from(base.subquery())
    )
    tokens_24h = await db.scalar(
        select(func.coalesce(func.sum(Job.output_tokens), 0)).where(
            Job.member_id == member_id,
            Job.completed_at > day_ago,
            Job.status == "completed",
        )
    )
    # Median-ish: SQLite would need percentile_cont; postgres has it but
    # not all dev DBs are pg. Use a pragmatic approximation: the simple
    # average of duration_ms instead. Real percentiles land when we have
    # enough data to care about distribution shape.
    avg_duration = await db.scalar(
        select(func.coalesce(func.avg(Job.duration_ms), 0)).where(
            Job.member_id == member_id,
            Job.completed_at > day_ago,
            Job.status == "completed",
            Job.duration_ms.isnot(None),
        )
    )

    return {
        "jobs_24h": int(jobs_24h or 0),
        "tokens_received_24h": int(tokens_24h or 0),
        "avg_duration_ms": int(avg_duration or 0),
    }


async def _finalize_job_in_db(
    *,
    job_id: uuid.UUID,
    terminal: Outcome,
    error_category: str | None,
    error_message: str | None,
    output_tokens: int,
    duration_ms: int,
) -> None:
    """Apply the ONE terminal outcome for a job and persist it.

    `terminal` is the authority and is required: it decides the row's status
    and whether credits mint. `error_category` / `error_message` are
    descriptive only, carried alongside for diagnostics. This split is the
    whole fix. Previously this function inferred success from
    `error_category is None`, so every exit that failed WITHOUT setting a
    category (a stream timeout, a client disconnect) was recorded as a
    completed job and minted consumption credits for work that never
    finished.

    Policy table:
      COMPLETED       -> status "completed"; output_tokens + duration_ms
                         written; credit-ledger rows written when the job is
                         cross-member and produced tokens.
      TIMEOUT         -> status "failed"; no credits.
      CANCELLED       -> status "failed"; no credits.
      PEER_ERROR      -> status "failed"; no credits.
      DISPATCH_FAILED -> status "failed"; no credits.

    No branch writes a reputation or quality signal. A failed job is not
    evidence of peer fault (see services/peer_registry.py::drain_peer_jobs on
    the "peer_disconnected" sentinel).

    Idempotent: calling it twice for the same job_id re-applies the same
    terminal state and, via the UNIQUE (job_id, direction) constraint from
    migration 0003, writes the ledger rows at most once.

    Terminal-state lock: once a row has reached a terminal status the FIRST
    outcome wins and a CONFLICTING later one is ignored and logged. Without
    this, two finalize sites racing the same row could relabel a completed,
    paid job as cancelled (leaving the row claiming it never finished while
    the ledger says it was paid) or promote a settled failure back to
    completed and mint for it. There is more than one finalize site now (the
    generator's `finally:`, and the settle-on-disconnect background task for
    a body that never ran), so the lock is what makes them safe to have.

    Runs as an independent task spawned from the streaming finalizer's
    `finally:` block so it survives the response generator's cancellation.
    All errors are logged; nothing propagates (the client is already gone)."""
    # Clamp the peer's self-reported output_tokens to the hard ceiling before it
    # touches the jobs row or the credit ledger. The peer is outside the trust
    # boundary (threat-model A2): nothing it reports is authoritative. This caps
    # the fabrication ceiling; it does not verify the work was real (see the
    # credit-verification design for sampled re-execution + reputation gating).
    if output_tokens > MAX_OUTPUT_TOKENS:
        logger.warning(
            "output_tokens_clamped",
            job_id=str(job_id),
            reported=output_tokens,
            clamped_to=MAX_OUTPUT_TOKENS,
        )
        output_tokens = MAX_OUTPUT_TOKENS
    if output_tokens < 0:
        output_tokens = 0

    try:
        async with get_session_factory()() as fresh_db:
            db_job = await fresh_db.get(Job, job_id)
            if db_job is None:
                logger.warning("job_finalize_row_missing", job_id=str(job_id))
                return

            # Terminal-state lock. A row that has already settled does not
            # move. Re-applying the SAME outcome still falls through (it is
            # idempotent, and it lets a retry finish a ledger write that
            # failed the first time); a different one is refused.
            if db_job.status in _TERMINAL_STATUSES and db_job.terminal_reason not in (
                None,
                terminal.value,
            ):
                logger.warning(
                    "job_finalize_conflict_ignored",
                    job_id=str(job_id),
                    settled_as=db_job.terminal_reason,
                    ignored=terminal.value,
                )
                return

            succeeded = terminal is Outcome.COMPLETED

            db_job.status = "completed" if succeeded else "failed"
            db_job.terminal_reason = terminal.value
            db_job.error_category = error_category
            if succeeded:
                # Only a completed job has billable work to report. Leaving
                # these NULL on every other outcome keeps the aggregates
                # honest and makes an unfinished job obvious in the row.
                db_job.output_tokens = output_tokens
                db_job.duration_ms = duration_ms
            db_job.completed_at = datetime.now(timezone.utc)
            await fresh_db.commit()

            if not succeeded:
                logger.info(
                    "job_finalized_failed",
                    job_id=str(job_id),
                    terminal=terminal.value,
                    error_category=error_category,
                    error_message=error_message,
                )

            # ---- Credit ledger write (COMPLETED path only) ---- #
            if succeeded and output_tokens > 0:
                try:
                    from coordinator.models.peer import Peer
                    from coordinator.services.credit_ledger import (
                        record_job_credits,
                    )

                    peer = await fresh_db.get(Peer, db_job.peer_id)
                    if peer is not None and peer.member_id == db_job.member_id:
                        # Self-served (My Machines / household): running on your
                        # own hardware is free — no spent/earned rows. Skipping
                        # the ledger also closes the credit-printing path the
                        # contribution bonus would open once My Machines is the
                        # default-preferred route (requester == owner would
                        # otherwise mint earned > spent for serving yourself).
                        logger.info("self_served_job_no_ledger", job_id=str(job_id))
                    elif peer is not None:
                        await record_job_credits(
                            fresh_db,
                            job_id=job_id,
                            requester_member_id=db_job.member_id,
                            contributor_owner_member_id=peer.member_id,
                            model_slug=db_job.model_slug,
                            output_tokens=output_tokens,
                            contributor_peer_id=db_job.peer_id,
                        )
                        await fresh_db.commit()
                        logger.info(
                            "credit_ledger_written",
                            job_id=str(job_id),
                            output_tokens=output_tokens,
                            model_slug=db_job.model_slug,
                        )
                except Exception:
                    logger.exception(
                        "credit_ledger_write_failed", job_id=str(job_id)
                    )
    except Exception:
        logger.exception("job_finalize_failed", job_id=str(job_id))


async def _first_token_deadline(
    db: AsyncSession, req: JobRequest, peer_conn: PeerConnection
) -> float:
    """How long THIS peer gets to produce a first token for THIS request.

    A flat deadline is a prompt-processing bug the moment prompts get long.
    A split 32B model measured ~22s to first token on a 2048-token prompt, so
    under the old flat 5s every such request burned all MAX_DISPATCH_ATTEMPTS
    and returned 503 while every peer involved was perfectly healthy.

    Estimate:
        est_prompt_tokens = (len(prompt) + sum(len(m.content) for messages))
                            / 4 chars-per-token
        expected_pp_rate  = measured PeerCapability.prompt_tokens_per_second
                            for (peer, model) when present and > 0, else its
                            tokens_per_second, else the coarse hardware-tier
                            proxy * 25 tok/s
        deadline          = clamp(FIRST_TOKEN_DEADLINE_S
                                  + est_prompt_tokens / expected_pp_rate,
                                  FIRST_TOKEN_DEADLINE_S, FIRST_TOKEN_MAX_S)

    FIRST_TOKEN_DEADLINE_S is read here as a module global, at CALL time, on
    purpose. Capturing it at import (a default argument, a module constant
    computed once) would silently ignore the tests that shrink it to keep
    dead-on-arrival scenarios fast.

    One query. `PeerConnection` carries only ids, so the peer's
    hardware_summary and its capability row for this model are fetched
    together via an outer join; a missing capability row simply falls back to
    the hardware-tier path.

    Nothing derived here crosses the wire. The deadline is a coordinator-side
    patience budget; the peer is never told about it (JobAssignedMessage is
    frozen at {type, job_id, model_slug, prompt, messages, sampling}).
    """
    from sqlalchemy import select as sa_select

    from coordinator.models.peer import Peer, PeerCapability
    from coordinator.services.peer_registry import _hardware_tier_score

    est_prompt_tokens = (
        len(req.prompt) + sum(len(m.content) for m in (req.messages or []))
    ) / _CHARS_PER_TOKEN

    row = (
        await db.execute(
            sa_select(
                PeerCapability.tokens_per_second,
                Peer.hardware_summary,
                PeerCapability.prompt_tokens_per_second,
            )
            .select_from(Peer)
            .outerjoin(
                PeerCapability,
                (PeerCapability.peer_id == Peer.id)
                & (PeerCapability.model_slug == req.model_slug),
            )
            .where(Peer.id == peer_conn.peer_id)
        )
    ).first()

    measured_tps = row[0] if row is not None else None
    hardware = (row[1] if row is not None else None) or {}
    measured_pp_tps = row[2] if row is not None else None

    # Prompt-processing rate is the right rate for a first-token budget;
    # generation rate is the next best measured figure; the tier guess last.
    if measured_pp_tps is not None and measured_pp_tps > 0:
        expected_pp_rate = float(measured_pp_tps)
    elif measured_tps is not None and measured_tps > 0:
        expected_pp_rate = float(measured_tps)
    else:
        expected_pp_rate = _hardware_tier_score(hardware) * _TIER_PP_TOKENS_PER_SECOND
    if expected_pp_rate <= 0:
        # _hardware_tier_score never returns <= 0 today; belt and braces so a
        # future tier constant can never turn this into a ZeroDivisionError.
        expected_pp_rate = _TIER_PP_TOKENS_PER_SECOND

    floor = FIRST_TOKEN_DEADLINE_S
    derived = floor + est_prompt_tokens / expected_pp_rate
    return max(floor, min(derived, FIRST_TOKEN_MAX_S))


async def _race_first_token(
    buf: JobBuffer, deadline_s: float
) -> tuple[str | None, bool]:
    """Wait for the FIRST of three outcomes on one dispatch attempt's buffer:

      1. A token lands on `buf.token_stream`      -> (token, False)
      2. `buf.done` fires with NO token yet        -> (None, True)
      3. `deadline_s` elapses with neither          -> (None, False)

    Implementation: two tasks (`buf.token_stream.get()` and `buf.done.wait()`)
    are raced via `asyncio.wait(..., return_when=FIRST_COMPLETED, timeout=...)`.

    Loser cleanup: whichever task is still pending when `asyncio.wait`
    returns is `.cancel()`-ed and awaited (swallowing the resulting
    CancelledError) so nothing is left dangling. This is safe for the
    queue getter specifically because `asyncio.Queue.get()` only pops an
    item off the internal deque as the LAST step of its own coroutine body,
    after its waiter future has already resolved — so `task.done() is False`
    is a hard guarantee that no item was silently consumed out from under a
    cancelled getter. Put differently: a token is only ever "spent" by
    actually returning it from this function.

    If BOTH tasks happen to be complete at the same time (e.g. the peer's
    JobCompleteMessage handler both enqueues nothing and sets `done` in the
    same event-loop tick), the token wins — outcome 1 is checked first,
    matching the "token arrived -> SUCCESS" priority in the spec.
    """
    get_task = asyncio.create_task(buf.token_stream.get())
    done_task = asyncio.create_task(buf.done.wait())
    try:
        finished, _pending = await asyncio.wait(
            {get_task, done_task},
            timeout=deadline_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if get_task in finished:
            return get_task.result(), False
        if done_task in finished:
            return None, True
        return None, False
    finally:
        for t in (get_task, done_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass


async def _finalize_abandoned_attempt(
    job: Job, buf: JobBuffer, deadline_s: float
) -> None:
    """Finalize an EARLIER dispatch attempt's Job row after failover moved on
    to a different peer — invariant #6 (no leaked 'dispatched' rows).

    Reuses `_finalize_job_in_db` with a non-COMPLETED terminal outcome, so:
      - the `jobs` row settles into `status='failed'` (not left dangling at
        'dispatched' forever), and
      - the credit-ledger write is gated OFF (invariant #2: a peer that never
        produced output earns no credit) — same code path a genuine
        peer-reported error takes.

    Awaited inline (not spawned as a detached background task) because this
    runs BEFORE the StreamingResponse is returned — we're still holding the
    request coroutine, so there's no cancellation hazard to design around
    (unlike the committed job's finalize in `dispatch_job_stream`'s
    `finally:`, which spawns detached because the client can disconnect the
    instant `[DONE]` is flushed).
    """
    if buf.error_category is not None:
        terminal = Outcome.PEER_ERROR
        error_category = buf.error_category
    else:
        terminal = Outcome.TIMEOUT
        error_category = "first_token_timeout"
    # `deadline_s` is the DERIVED per-attempt budget this peer actually got
    # (see `_first_token_deadline`), not the flat floor. Reporting the floor
    # here would misdescribe every long-prompt failover.
    error_message = buf.error_message or (
        f"peer produced no first token within {deadline_s:.1f}s; "
        f"coordinator failed over to another peer"
    )
    await _finalize_job_in_db(
        job_id=job.id,
        terminal=terminal,
        error_category=error_category,
        error_message=error_message,
        output_tokens=0,
        duration_ms=0,
    )


async def _dispatch_stream_with_failover(
    db: AsyncSession, req: JobRequest, member_id: uuid.UUID
) -> tuple[PeerConnection, Job, JobBuffer, RoutingScope, str | None]:
    """Resolve a peer that has actually produced a first token (or an
    empty-but-complete result) BEFORE the caller returns a StreamingResponse.

    HARD INVARIANT: this function must return (or raise) before a single
    byte reaches the client. Once it returns, the caller yields the stashed
    `first_token` and is COMMITTED to that peer — there is no failover once
    streaming has started, ever. See `dispatch_job_stream`.

    Loop up to MAX_DISPATCH_ATTEMPTS times, growing an `excluded` set of
    peer_ids so a peer that already proved dead-on-arrival for this request
    can never be re-picked (invariants #3 + #4). All three dead-on-arrival
    modes are handled the same way here: an error before the first token, a
    silent peer that misses its deadline, and a socket that was already dead
    when we tried to send it the assignment (`PeerDispatchFailed`).

    Worst-case pre-stream latency is MAX_DISPATCH_ATTEMPTS times the DERIVED
    per-attempt deadline from `_first_token_deadline` (so 3 * 5s for a short
    prompt, up to 3 * FIRST_TOKEN_MAX_S for a very long prompt on a very slow
    peer). Nothing has reached the client while this runs, so the cost is
    latency before the first byte, never a truncated stream.
    """
    excluded: set[uuid.UUID] = set()
    last_reason = "no eligible peer"

    for attempt in range(1, MAX_DISPATCH_ATTEMPTS + 1):
        try:
            peer_conn, job, buf, served_scope = await _dispatch_to_peer(
                db, req, member_id, exclude_peer_ids=excluded
            )
        except PeerDispatchFailed as exc:
            # DOA mode 3: the socket died between selection and send. The
            # attempt's Job row was already settled (DISPATCH_FAILED) inside
            # `_dispatch_to_peer`; all that is left is to keep the retry
            # budget alive instead of surfacing a 502 the client should never
            # have seen while other peers were available.
            last_reason = "dispatch_failed"
            logger.warning(
                "job_dispatch_send_failover",
                job_id=str(exc.job_id),
                peer_id=str(exc.peer_id),
                attempt=attempt,
                max_attempts=MAX_DISPATCH_ATTEMPTS,
                reason=last_reason,
            )
            excluded.add(exc.peer_id)
            continue

        # Per-attempt, per-peer patience budget: the flat floor plus this
        # peer's estimated prompt-processing time for this request.
        deadline_s = await _first_token_deadline(db, req, peer_conn)

        token, fired_done_first = await _race_first_token(buf, deadline_s)

        if token is not None:
            # SUCCESS. `token` has been consumed off `buf.token_stream` —
            # the caller MUST yield it before draining the rest of the queue.
            return peer_conn, job, buf, served_scope, token

        if fired_done_first and buf.error_category is None:
            # The peer completed with zero output tokens. An empty-but-valid
            # completion is a SUCCESS, not a failover trigger (invariant: do
            # NOT failover an empty-but-valid completion).
            return peer_conn, job, buf, served_scope, None

        # Dead on arrival: either the peer reported an error before any
        # token, or its DERIVED deadline elapsed with nothing at all.
        last_reason = "peer_error" if buf.error_category is not None else "first_token_timeout"
        logger.warning(
            "job_dispatch_first_token_failover",
            job_id=str(job.id),
            peer_id=str(peer_conn.peer_id),
            attempt=attempt,
            max_attempts=MAX_DISPATCH_ATTEMPTS,
            reason=last_reason,
        )
        # Free the in-flight slot immediately (idempotent — a no-op if the
        # peer-error path already called this) so scoring for OTHER
        # concurrent requests doesn't see a phantom job on this peer while
        # we retry.
        await complete_job(job.id)
        # Settle this attempt's Job row + skip credit (invariants #2 + #6).
        await _finalize_abandoned_attempt(job, buf, deadline_s)
        excluded.add(peer_conn.peer_id)

    raise HTTPException(
        status_code=503,
        detail=(
            f"No peer produced a first token after {MAX_DISPATCH_ATTEMPTS} "
            f"attempts (last reason: {last_reason})."
        ),
    )


@router.post("/stream")
async def dispatch_job_stream(
    req: JobRequest,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> StreamingResponse:
    """Stream tokens from a peer back to the requester as Server-Sent Events.

    Same dispatch semantics as POST /jobs (capability-aware peer pick,
    blinded routing, 503 on no eligible peer) but the response is an
    SSE stream rather than a collected JSON body.

    Active first-token failover: dispatch doesn't commit to a peer until
    that peer has actually produced its first token (or completed empty).
    A peer that hangs, black-holes, or errors before its first token never
    gets a byte in front of the client — `_dispatch_stream_with_failover`
    retries a different peer instead, up to MAX_DISPATCH_ATTEMPTS. Once
    ANY token has been yielded below, we are committed: no retry, ever.
    """
    # ---- 1-3. Pick peer, create job, deliver, AND wait for the first token
    # (or empty-complete) before this function returns anything to ASGI. If
    # every attempt dies before producing output, this raises HTTPException
    # (still works as a normal error response — no StreamingResponse exists
    # yet). ---- #
    peer_conn, job, buf, served_scope, first_token = await _dispatch_stream_with_failover(
        db, req, member_id
    )
    job_id = job.id
    logger.info(
        "job_dispatched_stream",
        job_id=str(job_id),
        peer_id=str(peer_conn.peer_id),
        model_slug=req.model_slug,
        prompt_chars=len(req.prompt),
    )

    # ---- 4. Stream the response ---- #
    #
    # Shared with the settle-on-disconnect hook below. This must be created
    # before the generator so both sides close over the same object.
    stream_start = _StreamStart()

    async def _generate() -> AsyncGenerator[str, None]:
        # FIRST statement, above every await and every yield: from here on the
        # `finally:` at the bottom is guaranteed to run and owns settlement,
        # so `_settle_if_never_started` must stand down.
        stream_start.started = True

        deadline = asyncio.get_running_loop().time() + JOB_TIMEOUT_S
        # The ONE terminal outcome for this job. Defaults to COMPLETED and is
        # narrowed by whichever exit the stream actually takes; exactly this
        # value reaches `_finalize_job_in_db` below.
        outcome = Outcome.COMPLETED
        # True once the wire has carried its terminal envelope. A client that
        # hangs up AFTER that point already has its whole answer, so a late
        # GeneratorExit must not rewrite a finished job as CANCELLED.
        terminal_wire_sent = False
        try:
            try:
                # The first token (if any) was already consumed off the queue by
                # _dispatch_stream_with_failover while deciding which peer to
                # commit to. Yield it now, before entering the normal pull loop,
                # so the client sees it exactly once and in order. From this
                # line onward we are irreversibly committed to `peer_conn` --
                # nothing below ever re-dispatches.
                if first_token is not None:
                    yield _sse_chunk(first_token)

                while True:
                    # Bail out cleanly when the peer has signalled completion
                    # AND the queue has drained -- no point waiting on a put
                    # that will never come.
                    if buf.done.is_set() and buf.token_stream.empty():
                        break

                    if buf.done.is_set():
                        # The peer has FINISHED and the rest of its answer is
                        # already in the coordinator's own queue. Deliver it
                        # even if the deadline has passed: JOB_TIMEOUT_S is
                        # patience for a peer that is still generating, and
                        # spending it here would discard work that exists,
                        # tell the client its answer failed after most of it
                        # arrived, and deny the contributor credit for a job
                        # it completed. Bounded by the queue's own size (and
                        # the buffer is already unregistered once `done` is
                        # set, so nothing can refill it), which is why this
                        # cannot become an unbounded extension of the
                        # deadline.
                        for _ in range(buf.token_stream.qsize()):
                            try:
                                backlog_token = buf.token_stream.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            yield _sse_chunk(backlog_token)
                        continue

                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        # The peer hung mid-stream. That is a terminal
                        # FAILURE, not a completion. Recording the outcome
                        # here is what stops the finalizer below from
                        # settling the row as "completed" and minting credit
                        # for a job that produced nothing usable.
                        outcome = Outcome.TIMEOUT
                        break

                    try:
                        token = await asyncio.wait_for(
                            buf.token_stream.get(), timeout=min(remaining, 1.0)
                        )
                        yield _sse_chunk(token)
                    except asyncio.TimeoutError:
                        # Inner timeout = no token in the last ~1s. Loop back
                        # to check `done` so we exit promptly when the peer
                        # finishes between tokens (or the deadline expires).
                        continue

                # ---- Terminal envelope ---- #
                #
                # Wire contract: an error envelope is authoritative and is
                # never followed by meta. A meta envelope after an error reads
                # to a naive client as a successful completion, which is
                # exactly how a timed-out job used to look on the wire. Both
                # failure envelopes are still followed by [DONE] so a client
                # that only knows how to watch for [DONE] terminates.
                # `terminal_wire_sent` is set BEFORE each of these yields,
                # not after. The generator parks ON a yield, so setting it
                # afterwards left one suspension point (the meta yield) at
                # which the client already had every token AND the completion
                # summary while the guard below still read the close as a
                # mid-stream abort and rewrote a delivered, finished job as
                # cancelled with no credit.
                if outcome is Outcome.TIMEOUT:
                    terminal_wire_sent = True
                    yield _sse_error(
                        "timeout", f"Peer didn't complete within {JOB_TIMEOUT_S}s"
                    )
                elif buf.error_category is not None:
                    outcome = Outcome.PEER_ERROR
                    terminal_wire_sent = True
                    yield _sse_error(
                        buf.error_category,
                        buf.error_message or "(no message)",
                    )
                else:
                    terminal_wire_sent = True
                    yield _sse_meta(
                        output_tokens=buf.output_tokens,
                        duration_ms=buf.duration_ms,
                        finish_reason=buf.finish_reason or "stop",
                        served_scope=served_scope,
                    )
                yield _sse_done()
            except (asyncio.CancelledError, GeneratorExit):
                # The client went away mid-stream: uvicorn cancels the
                # response task, starlette aclose()s the generator. The job
                # did NOT complete, so it must not be recorded as if it had.
                # Only claim the cancellation when the stream had not already
                # reached a terminal state of its own.
                if not terminal_wire_sent and outcome is Outcome.COMPLETED:
                    outcome = Outcome.CANCELLED
                raise
        finally:
            # ---- Finalize FIRST, await SECOND ---- #
            #
            # Capture the buffer's fields and spawn the finalize task BEFORE
            # any await in this block. Under cancellation every await here is
            # a potential raise point (a contended lock inside complete_job()
            # is exactly such a point), and a raise above the create_task
            # would leave the row at 'dispatched' with no finalization at
            # all. Ordering is the whole guarantee.
            #
            # The task is detached on purpose, for two reasons:
            #   1. The request-scoped `db` from Depends(get_db) is already
            #      closed by the time this runs, so any commit on it would
            #      silently no-op. `_finalize_job_in_db` opens its own
            #      session.
            #   2. It owns its own cancel scope, so it runs to completion
            #      regardless of what happens to the response task. Errors
            #      are logged, never raised at the (already gone) client.
            captured_tokens = buf.output_tokens
            captured_duration = buf.duration_ms
            captured_category = buf.error_category
            captured_message = buf.error_message

            if outcome is Outcome.COMPLETED and captured_category is not None:
                # An error landed on the buffer after the drain check (peer
                # disconnect racing the last token). Success is the one
                # outcome that mints, so resolve the race against minting.
                outcome = Outcome.PEER_ERROR
            if outcome is Outcome.TIMEOUT:
                captured_category = captured_category or "timeout"
                captured_message = captured_message or (
                    f"peer did not complete within {JOB_TIMEOUT_S}s"
                )
            elif outcome is Outcome.CANCELLED:
                captured_category = captured_category or "client_disconnected"
                captured_message = captured_message or (
                    "requester closed the stream before the job finished"
                )

            _task = asyncio.create_task(
                _finalize_job_in_db(
                    job_id=job_id,
                    terminal=outcome,
                    error_category=captured_category,
                    error_message=captured_message,
                    output_tokens=captured_tokens,
                    duration_ms=captured_duration,
                )
            )
            _background_tasks.add(_task)
            _task.add_done_callback(_background_tasks.discard)

            # complete_job() is idempotent -- the peer side may have already
            # signalled this when JobCompleteMessage landed. It frees the
            # in-flight slot for scoring; it is deliberately below the
            # create_task because it is the first thing here that can await.
            await complete_job(job_id)

    async def _settle_if_never_started() -> None:
        """Settle a job whose response body never ran.

        This is the one exit with no code on it. `_dispatch_stream_with_failover`
        has already committed the Job row as 'dispatched' and registered the
        in-flight buffer, and it may have spent up to MAX_DISPATCH_ATTEMPTS
        times the derived deadline doing so with not one byte on the wire, so
        it is also the widest window in the whole path for a requester to give
        up. When they do, starlette cancels `stream_response` before its first
        `async for`, the generator body never executes, and with it neither
        the `finally:` that settles the row nor the `complete_job` that frees
        the in-flight slot. The row would sit at 'dispatched' with a NULL
        terminal_reason forever (there is no job reaper), and the phantom
        in-flight job would count against the peer's routing score until it
        disconnects.

        Deterministic, never GC-driven: starlette awaits this as the
        response's BackgroundTask after its task group exits, on both normal
        completion and client disconnect, and `_GuardedStream.aclose` calls it
        for a body closed without being iterated.

        No-op when the generator did run, and no-op on a second call.
        """
        if stream_start.started or stream_start.settled:
            return
        stream_start.settled = True
        logger.warning(
            "job_stream_never_started",
            job_id=str(job_id),
            peer_id=str(peer_conn.peer_id),
            reason="client_disconnected_before_first_chunk",
        )
        await complete_job(job_id)
        await _finalize_job_in_db(
            job_id=job_id,
            terminal=Outcome.CANCELLED,
            error_category="client_disconnected",
            error_message="requester disconnected before the first chunk was sent",
            output_tokens=0,
            duration_ms=0,
        )

    return StreamingResponse(
        _GuardedStream(_generate(), _settle_if_never_started),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
        background=BackgroundTask(_settle_if_never_started),
    )
