"""OpenAI Responses API on the gateway, so Codex CLI and other Responses clients can
use a model running on the user's own machines.

Endpoint:
  POST /v1/responses   (streaming + non-streaming)

Point a client at  http://127.0.0.1:8321/v1  with the gateway key as its API key
(Authorization: Bearer, checked in app.py, which also answers a missing key in this
API's error shape).

There is no routing code here. The body is translated into the same ChatJob the
other routes build (services/responses_api.py) and run through routers/gateway.py:
this machine, a household peer, a circle or the pool, chosen by the model and the
X-Potluck-Scope and X-Potluck-Circle headers. The reply is translated back at the
edge, and the gateway's X-Potluck-* response headers stay. Usage figures the engine
did not give are estimated, and then the reply carries X-Potluck-Usage: estimated. A
streamed reply always does, because the headers go out before the counts are known.
Tool types that OpenAI hosts (web_search and the like) cannot run here; they are left
out of the request and named in X-Potluck-Ignored-Tools.

Every error from this route has OpenAI's shape, {"error": {"message", "type", "param",
"code"}}, with the status the other gateway routes would have used. A stream that
fails after it has begun ends with a response.failed event carrying the same error. A
prompt longer than the model's context has the code context_length_exceeded.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from potluck_local.routers import anthropic as shared
from potluck_local.routers import gateway
from potluck_local.services import anthropic_messages as am
from potluck_local.services import gateway_request, inference_errors, token_estimate
from potluck_local.services import responses_api as ra

logger = logging.getLogger(__name__)

router = APIRouter()

# How long a stream waits for the engine's first word before it begins anyway.
# A failure inside this window is an HTTP error; after it, a response.failed event.
COMMIT_WINDOW_S = shared.COMMIT_WINDOW_S
KEEPALIVE_INTERVAL_S = ra.KEEPALIVE_INTERVAL_S
_COUNTED_EVENTS = (
    "event: response.output_text.delta",
    "event: response.function_call_arguments.delta",
    "event: response.custom_tool_call_input.delta",
)


def _error_response(status: int, detail: Any, headers: dict | None = None) -> JSONResponse:
    message, code = am.error_detail(detail)
    extra = dict(headers or {})
    if code:
        extra["X-Potluck-Error"] = code
    return JSONResponse(ra.error_body(status, message, code), status_code=status, headers=extra)


async def _parse(request: Request) -> ra.ResponsesRequest:
    """The request body, read and checked. Parsed here, not by FastAPI, so a bad
    body is answered in OpenAI's error shape and not as a 422."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > ra.MAX_BODY_BYTES:
        raise HTTPException(413, "The request body is too large.")
    raw = await request.body()
    if len(raw) > ra.MAX_BODY_BYTES:
        raise HTTPException(413, "The request body is too large.")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise HTTPException(400, {"code": "invalid_json", "message": "The request body is not valid JSON."}) from None
    try:
        return ra.parse_request(body, default_temperature=gateway.DEFAULT_TEMPERATURE)
    except gateway_request.RequestError as exc:
        raise HTTPException(400, {"code": exc.code, "message": exc.message}) from exc


def _too_long(cause: BaseException | None) -> HTTPException | None:
    found = gateway.context_overflow(cause)
    if found is None:
        return None
    return HTTPException(400, {"code": "context_length_exceeded", "message": ra.context_length_message(*found)})


def _describe(failure: object, plan: gateway.ChatPlan) -> am.Failure:
    """A failure of the engine, as the status and message to tell the client."""
    found = gateway.context_overflow(failure)
    if found is not None:
        return am.Failure(400, ra.context_length_message(*found), "context_length_exceeded")
    if isinstance(failure, dict):
        # This machine's own model: the chunk carries its message and category.
        text = str(failure.get("__error__") or "inference failed")
        category = failure.get("__category__")
        category = category if isinstance(category, str) else None
        status = 503 if category == inference_errors.GPU_OUT_OF_MEMORY else 500
        return am.Failure(status, text, category)
    exc = gateway.http_failure(failure, plan)  # type: ignore[arg-type]
    message, code = am.error_detail(exc.detail)
    return am.Failure(exc.status_code, message, code)


async def _metered(events: AsyncIterator[str], plan: gateway.ChatPlan) -> AsyncIterator[str]:
    """Count delta events as output tokens, the way the OpenAI route counts chunks,
    then record one consumed usage row. Only the event name is read, never the
    content. Records in finally so a client disconnect still lands."""
    started = time.monotonic()
    first_ms: int | None = None
    count = 0
    ok = True
    try:
        async for event in events:
            if event.startswith(_COUNTED_EVENTS):
                if first_ms is None:
                    first_ms = int((time.monotonic() - started) * 1000)
                count += 1
            elif event.startswith("event: response.failed"):
                ok = False
            yield event
    finally:
        await gateway.record_usage(
            plan, output_tokens=count, ttft_ms=first_ms,
            duration_ms=int((time.monotonic() - started) * 1000), ok=ok,
        )


async def _create_response(request: Request, x_potluck_scope: str | None, x_potluck_circle: str | None):
    parsed = await _parse(request)
    try:
        plan = await gateway.plan_chat(
            gateway.ChatJob(parsed.prepared, parsed.model, parsed.temperature, parsed.max_tokens, parsed.stream),
            x_potluck_scope, x_potluck_circle,
        )
    except HTTPException as exc:
        logger.info("gateway_responses_refused status=%d model=%s", exc.status_code, am.shown_name(parsed.model))
        raise
    logger.info(
        "gateway_responses stream=%s tools=%d ignored_tools=%d first_call_only=%s",
        parsed.stream, len(parsed.prepared.tools or ()), len(parsed.ignored_tools), parsed.first_call_only,
    )
    text = ra.prompt_text(parsed)
    model = parsed.model or plan.served_model or ""
    headers = {"X-Potluck-Served-By": plan.served_by}
    if parsed.ignored_tools:
        headers["X-Potluck-Ignored-Tools"] = ",".join(dict.fromkeys(parsed.ignored_tools))

    if not parsed.stream:
        try:
            result = await gateway.complete_chat(plan)
        except HTTPException as exc:
            raise (_too_long(exc.__cause__) or exc) from exc.__cause__
        reply, estimated = ra.response_from_completion(result, parsed, model, token_estimate.estimate_tokens(text))
        return JSONResponse(reply, headers={**headers, **({"X-Potluck-Usage": "estimated"} if estimated else {})})

    stream = ra.ResponseStream(
        gateway.shaped_chunks(plan), parsed, model, await shared._input_tokens(plan, text),
        lambda failure: _describe(failure, plan), keepalive_interval=KEEPALIVE_INTERVAL_S,
    )
    try:
        failure = await stream.start(COMMIT_WINDOW_S)
    except BaseException:
        await stream.aclose()
        raise
    if failure is not None:
        await stream.aclose()
        raise HTTPException(failure.status, {"code": failure.code, "message": failure.message}
                            if failure.code else failure.message)
    return StreamingResponse(
        _metered(stream.events(), plan),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
            **headers, "X-Potluck-Usage": "estimated",
        },
    )


@router.post("/responses")
async def create_response(
    request: Request,
    x_potluck_scope: str | None = Header(default=None),
    x_potluck_circle: str | None = Header(default=None),
):
    """OpenAI POST /v1/responses."""
    try:
        return await _create_response(request, x_potluck_scope, x_potluck_circle)
    except HTTPException as exc:
        return _error_response(exc.status_code, exc.detail, exc.headers)
    except Exception:  # noqa: BLE001
        logger.exception("gateway_responses_failed")
        return _error_response(500, "The engine hit an unexpected error.")
