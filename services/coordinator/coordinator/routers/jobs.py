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

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.db.database import get_db, get_session_factory
from coordinator.models.job import Job
from coordinator.schemas.peer import (
    JobAssignedMessage,
    SamplingParams,
)
from coordinator.services.peer_registry import (
    complete_job,
    pick_peer_for_model,
    register_job,
)

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["jobs"])

# Per-job hard ceiling. Even a 512-token completion at 5 tok/s on a slow
# peer should finish well under this. If we hit it, the peer hung and we
# should stop blocking the requesting client.
JOB_TIMEOUT_S = 120.0

# Stub-only constant — m3 dispatch doesn't yet have member auth wired up
# from the desktop side. Real member_id comes from the authenticated session
# in v0.5; this matches the dev-fixture seed already in contributor_releases.
_STUB_MEMBER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class JobRequest(BaseModel):
    prompt: str
    model_slug: str = "stub-echo"  # m3 stub default; real models when llama-cpp lands
    sampling: SamplingParams = Field(default_factory=SamplingParams)


class JobResponse(BaseModel):
    job_id: uuid.UUID
    response: str
    output_tokens: int
    duration_ms: int
    finish_reason: str
    # peer_id is intentionally omitted from the response — exposing it to
    # the requester would let them shop for a specific peer, which breaks
    # the anonymity guarantee the manifesto promises.


class JobErrorResponse(BaseModel):
    job_id: uuid.UUID
    error_category: str
    error_message: str


@router.post("", response_model=JobResponse, responses={502: {"model": JobErrorResponse}})
async def dispatch_job(
    req: JobRequest,
    db: AsyncSession = Depends(get_db),
) -> JobResponse:
    """Dispatch one inference job to an online peer and return the full
    response when it completes.

    Synchronous: this endpoint blocks until the peer finishes (or
    JOB_TIMEOUT_S elapses). Good enough for the m3 stub smoke test.
    Streaming endpoints come in a follow-up.
    """
    # ---- 1. Pick a peer that actually serves this model ---- #
    peer_conn = await pick_peer_for_model(db, req.model_slug)
    if peer_conn is None:
        # Distinguish "nobody online" from "nobody online serves this model"
        # so the caller knows whether to wait or pick a different slug.
        from coordinator.services.peer_registry import online_peer_count
        online = await online_peer_count()
        if online == 0:
            detail = "No contributor peers connected. Bring at least one online and retry."
        else:
            detail = (
                f"No online peer serves model '{req.model_slug}'. "
                f"{online} peer(s) online but none advertise that capability. "
                f"Either pick a different model_slug or wait for an eligible peer to connect."
            )
        raise HTTPException(status_code=503, detail=detail)

    # ---- 2. Create the Job DB row ---- #
    job = Job(
        member_id=_STUB_MEMBER_ID,
        peer_id=peer_conn.peer_id,
        model_slug=req.model_slug,
        privacy_mode="pseudonymous",
        status="dispatched",
    )
    db.add(job)
    await db.flush()  # populates job.id
    job_id = job.id
    await db.commit()

    # ---- 3. Register the job buffer + send JobAssigned ---- #
    buf = await register_job(job_id)

    # CRITICAL PRIVACY POINT: JobAssignedMessage carries ONLY job_id +
    # model_slug + prompt + sampling. Adding member_id or any identifying
    # field here is a blinded-routing violation. The schema for
    # JobAssignedMessage doesn't have a member_id field, so this is also
    # caught at type-check time, but the rule deserves the comment.
    assigned = JobAssignedMessage(
        job_id=job_id,
        model_slug=req.model_slug,
        prompt=req.prompt,
        sampling=req.sampling,
    )

    try:
        await peer_conn.ws.send_json(assigned.model_dump(mode="json"))
    except Exception as exc:
        # Couldn't reach the peer — mark the job failed and bail.
        logger.warning(
            "job_dispatch_send_failed",
            job_id=str(job_id),
            peer_id=str(peer_conn.peer_id),
            reason=str(exc),
        )
        await complete_job(job_id)
        raise HTTPException(
            status_code=502,
            detail=f"Couldn't deliver job to peer: {exc}",
        ) from exc

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


def _sse_meta(output_tokens: int, duration_ms: int, finish_reason: str) -> str:
    """Encode a job-completion summary as an SSE chunk.

    The frontend uses this to show "served by a verified contributor"
    metadata under network-mode chat messages without exposing peer
    identity. `served_by: "contributor"` is the only signal — peer_id,
    hostname, and any other identifying fields are deliberately absent
    per the manifesto's blinded-routing invariant (memory 42a7e671).
    """
    payload = {
        "meta": {
            "served_by": "contributor",
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


@router.get("/stats")
async def my_job_stats(db: AsyncSession = Depends(get_db)) -> dict:
    """Per-member requester stats for the desktop /network view.

    v0 hardcodes member_id to the dev-fixture stub. When real auth lands
    in v0.5 this filters by the authenticated session's member_id instead.

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
        Job.member_id == _STUB_MEMBER_ID,
        Job.completed_at > day_ago,
        Job.status == "completed",
    )

    jobs_24h = await db.scalar(
        select(func.count()).select_from(base.subquery())
    )
    tokens_24h = await db.scalar(
        select(func.coalesce(func.sum(Job.output_tokens), 0)).where(
            Job.member_id == _STUB_MEMBER_ID,
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
            Job.member_id == _STUB_MEMBER_ID,
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
                    if peer is not None:
                        await record_job_credits(
                            fresh_db,
                            job_id=job_id,
                            requester_member_id=db_job.member_id,
                            contributor_owner_member_id=peer.member_id,
                            model_slug=db_job.model_slug,
                            output_tokens=output_tokens,
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


@router.post("/stream")
async def dispatch_job_stream(
    req: JobRequest,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream tokens from a peer back to the requester as Server-Sent Events.

    Same dispatch semantics as POST /jobs (capability-aware peer pick,
    blinded routing, 503 on no eligible peer) but the response is an
    SSE stream rather than a collected JSON body.
    """
    # ---- 1. Pick a peer that serves this model ---- #
    peer_conn = await pick_peer_for_model(db, req.model_slug)
    if peer_conn is None:
        from coordinator.services.peer_registry import online_peer_count
        online = await online_peer_count()
        if online == 0:
            detail = "No contributor peers connected. Bring at least one online and retry."
        else:
            detail = (
                f"No online peer serves model '{req.model_slug}'. "
                f"{online} peer(s) online but none advertise that capability."
            )
        raise HTTPException(status_code=503, detail=detail)

    # ---- 2. Create the Job DB row ---- #
    job = Job(
        member_id=_STUB_MEMBER_ID,
        peer_id=peer_conn.peer_id,
        model_slug=req.model_slug,
        privacy_mode="pseudonymous",
        status="dispatched",
    )
    db.add(job)
    await db.flush()
    job_id = job.id
    await db.commit()

    # ---- 3. Register buffer + send JobAssigned ---- #
    buf = await register_job(job_id)

    assigned = JobAssignedMessage(
        job_id=job_id,
        model_slug=req.model_slug,
        prompt=req.prompt,
        sampling=req.sampling,
    )
    try:
        await peer_conn.ws.send_json(assigned.model_dump(mode="json"))
    except Exception as exc:
        await complete_job(job_id)
        raise HTTPException(
            status_code=502,
            detail=f"Couldn't deliver job to peer: {exc}",
        ) from exc

    logger.info(
        "job_dispatched_stream",
        job_id=str(job_id),
        peer_id=str(peer_conn.peer_id),
        model_slug=req.model_slug,
        prompt_chars=len(req.prompt),
    )

    # ---- 4. Stream the response ---- #
    async def _generate() -> AsyncGenerator[str, None]:
        deadline = asyncio.get_event_loop().time() + JOB_TIMEOUT_S
        try:
            while True:
                # Bail out cleanly when the peer has signalled completion
                # AND the queue has drained — no point waiting on a put
                # that will never come.
                if buf.done.is_set() and buf.token_stream.empty():
                    break

                remaining = deadline - asyncio.get_event_loop().time()
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

            asyncio.create_task(
                _finalize_job_in_db(
                    job_id=job_id,
                    error_category=captured_error,
                    error_message=captured_error_msg,
                    output_tokens=captured_tokens,
                    duration_ms=captured_duration,
                )
            )

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
