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
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Literal, Self

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
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
FIRST_TOKEN_DEADLINE_S = 5.0

# Hard ceiling on how many peers we'll try for a single streaming request
# before giving up and returning 503. Bounds worst-case dispatch latency to
# roughly MAX_DISPATCH_ATTEMPTS * FIRST_TOKEN_DEADLINE_S.
MAX_DISPATCH_ATTEMPTS = 3


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
    # Circle scope: validate ownership then restrict candidates to circle members.
    circle_pubkeys: set[str] | None = None
    if req.scope == "circle":
        if req.circle_id is None:
            raise HTTPException(
                status_code=422,
                detail="circle_id is required when scope='circle'.",
            )
        # Verify the circle belongs to the caller before doing anything else.
        from coordinator.models.circle import TrustedCircle, CircleMember
        from sqlalchemy import select as sa_select

        circle_row = await db.get(TrustedCircle, req.circle_id)
        if circle_row is None or circle_row.owner_member_id != member_id:
            raise HTTPException(
                status_code=404,
                detail="circle not found",
            )
        # Collect the pubkeys in this circle.
        cm_result = await db.execute(
            sa_select(CircleMember.member_pubkey).where(
                CircleMember.circle_id == req.circle_id
            )
        )
        circle_pubkeys = {row[0] for row in cm_result.all()}

    peer_conn, served_scope = await select_peer_for_request(
        db,
        model_slug=req.model_slug,
        requester_member_id=member_id,
        scope=req.scope,
        circle_pubkeys=circle_pubkeys,
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
        raise HTTPException(
            status_code=502,
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
        raise HTTPException(
            status_code=504,
            detail=f"Peer didn't complete within {JOB_TIMEOUT_S}s",
        )

    # ---- 5. Return result OR error ---- #
    if buf.error_category is not None:
        # Peer sent JobErrorMessage. Update the Job row + return 502.
        job.status = "failed"
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(
            status_code=502,
            detail={
                "job_id": str(job_id),
                "error_category": buf.error_category,
                "error_message": buf.error_message or "(no message)",
            },
        )

    response_text = "".join(buf.tokens)

    job.status = "completed"
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
    error_category: str | None,
    error_message: str | None,
    output_tokens: int,
    duration_ms: int,
) -> None:
    """Persist final job state + credit-ledger rows. Runs as an independent
    task spawned from the streaming finalizer's `finally:` block so it
    survives the response generator's cancellation.

    Two writes happen here, in order:
      1. Mark the `jobs` row complete (status, output_tokens, duration,
         completed_at).
      2. If the job succeeded with non-zero output tokens, write the
         two credit-ledger rows (spent for requester, earned for
         contributor's owner). Per the asymmetric earn/spend math in
         services/credit_ledger.py — earn = spent × 1.5.

    All errors are logged at WARN; nothing propagates (the client is
    already gone)."""
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

            db_job.status = "failed" if error_category is not None else "completed"
            if error_category is None:
                db_job.output_tokens = output_tokens
                db_job.duration_ms = duration_ms
            db_job.completed_at = datetime.now(timezone.utc)
            await fresh_db.commit()

            # ---- Credit ledger write (success path only) ---- #
            if error_category is None and output_tokens > 0:
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


async def _finalize_abandoned_attempt(job: Job, buf: JobBuffer) -> None:
    """Finalize an EARLIER dispatch attempt's Job row after failover moved on
    to a different peer — invariant #6 (no leaked 'dispatched' rows).

    Reuses `_finalize_job_in_db` with `error_category` always non-None, so:
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
    error_category = buf.error_category or "first_token_timeout"
    error_message = buf.error_message or (
        f"peer produced no first token within {FIRST_TOKEN_DEADLINE_S}s; "
        f"coordinator failed over to another peer"
    )
    await _finalize_job_in_db(
        job_id=job.id,
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
    can never be re-picked (invariants #3 + #4).
    """
    excluded: set[uuid.UUID] = set()
    last_reason = "no eligible peer"

    for attempt in range(1, MAX_DISPATCH_ATTEMPTS + 1):
        peer_conn, job, buf, served_scope = await _dispatch_to_peer(
            db, req, member_id, exclude_peer_ids=excluded
        )

        token, fired_done_first = await _race_first_token(buf, FIRST_TOKEN_DEADLINE_S)

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
        # token, or FIRST_TOKEN_DEADLINE_S elapsed with nothing at all.
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
        await _finalize_abandoned_attempt(job, buf)
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
    async def _generate() -> AsyncGenerator[str, None]:
        deadline = asyncio.get_running_loop().time() + JOB_TIMEOUT_S
        try:
            # The first token (if any) was already consumed off the queue by
            # _dispatch_stream_with_failover while deciding which peer to
            # commit to. Yield it now, before entering the normal pull loop,
            # so the client sees it exactly once and in order. From this
            # line onward we are irreversibly committed to `peer_conn` —
            # nothing below ever re-dispatches.
            if first_token is not None:
                yield _sse_chunk(first_token)

            while True:
                # Bail out cleanly when the peer has signalled completion
                # AND the queue has drained — no point waiting on a put
                # that will never come.
                if buf.done.is_set() and buf.token_stream.empty():
                    break

                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    yield _sse_error("timeout", f"Peer didn't complete within {JOB_TIMEOUT_S}s")
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

            # Stream drained. Translate any error the peer reported into
            # an SSE error chunk; otherwise emit a final meta envelope
            # (output_tokens, duration_ms, served_by) and then DONE.
            if buf.error_category is not None:
                yield _sse_error(
                    buf.error_category,
                    buf.error_message or "(no message)",
                )
            else:
                yield _sse_meta(
                    output_tokens=buf.output_tokens,
                    duration_ms=buf.duration_ms,
                    finish_reason=buf.finish_reason or "stop",
                    served_scope=served_scope,
                )
                yield _sse_done()
        finally:
            # complete_job() is idempotent — peer side may have already
            # signalled this when JobCompleteMessage landed.
            await complete_job(job_id)

            # ---- Finalize as a DETACHED background task ---- #
            #
            # Two intertwined problems if we await the DB writes here:
            #
            # 1. The request-scoped `db` from Depends(get_db) closes when
            #    the StreamingResponse hands the generator off to ASGI,
            #    so any commit on that session silently no-ops. We solve
            #    this by opening a fresh session below.
            # 2. When the client closes the connection (which happens
            #    naturally right after `[DONE]`), uvicorn cancels the
            #    response task. Any `await` from inside this `finally`
            #    block then raises CancelledError, leaving the job in
            #    'dispatched' and credits unwritten.
            #
            # The fix is to spawn the finalize as an independent task
            # via `asyncio.create_task` — it owns its own cancel scope
            # and runs to completion regardless of the streaming response's
            # cancellation. Errors are logged but never propagated back
            # to the (already-closed) client.
            captured_error = buf.error_category
            captured_error_msg = buf.error_message
            captured_tokens = buf.output_tokens
            captured_duration = buf.duration_ms

            _task = asyncio.create_task(
                _finalize_job_in_db(
                    job_id=job_id,
                    error_category=captured_error,
                    error_message=captured_error_msg,
                    output_tokens=captured_tokens,
                    duration_ms=captured_duration,
                )
            )
            _background_tasks.add(_task)
            _task.add_done_callback(_background_tasks.discard)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
