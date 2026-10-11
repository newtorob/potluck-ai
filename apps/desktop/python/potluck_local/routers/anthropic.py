"""Anthropic Messages API on the gateway, so the Anthropic SDKs and Claude Code can
use a model running on the user's own machines.

Endpoints:
  POST /v1/messages               (streaming + non-streaming)
  POST /v1/messages/count_tokens  (never loads a model)

Point an Anthropic client at  http://127.0.0.1:8321  (its base URL, without /v1)
with the gateway key as its API key. The key may arrive as x-api-key or as
Authorization: Bearer (checked in app.py, which also answers a missing key in this
API's error shape).

There is no routing code here. The body is translated into the same ChatJob the
OpenAI route builds (services/anthropic_messages.py) and run through
routers/gateway.py: this machine, a household peer, a circle or the pool, chosen
by the model and the X-Potluck-Scope and X-Potluck-Circle headers. The reply is
translated back at the edge, and the gateway's X-Potluck-* response headers stay.
Usage figures the engine did not give are estimated, and then the reply carries
X-Potluck-Usage: estimated. A streamed reply always does, because the headers go
out before the counts are known.

Every error from these routes has Anthropic's shape, {"type": "error", "error":
{"type", "message"}}, with the status the OpenAI route would have used. When the
detail carries a code it is returned in X-Potluck-Error.

A prompt longer than the model's context is a 400 "prompt is too long: N tokens >
M maximum", the words Claude Code looks for. The engine finds that out when it
starts; this machine's model answers within a moment, so the 400 is a real HTTP
error. A model served by another machine reports only "Inference failed" (the
household route sends fixed text), so a prompt that is too long there is a plain
inference failure, not this 400.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from potluck_local.routers import gateway
from potluck_local.services import anthropic_messages as am
from potluck_local.services import gateway_request, inference_errors

logger = logging.getLogger(__name__)

router = APIRouter()

# How long a stream waits for the engine's first word before it begins anyway.
# A failure inside this window is an HTTP error; after it, an error event.
COMMIT_WINDOW_S = 3.0
# How long a token count may wait for the tokenizer before a length estimate is used.
COUNT_WAIT_S = 0.5
PING_INTERVAL_S = am.PING_INTERVAL_S


def _error_response(status: int, detail: Any, headers: dict | None = None) -> JSONResponse:
    message, code = am.error_detail(detail)
    extra = dict(headers or {})
    if code:
        extra["X-Potluck-Error"] = code
    return JSONResponse(am.error_body(status, message), status_code=status, headers=extra)


async def _parse(request: Request) -> am.MessagesRequest:
    """The request body, read and checked. Parsed here, not by FastAPI, so a bad
    body is answered in Anthropic's error shape and not as a 422."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > am.MAX_BODY_BYTES:
        raise HTTPException(413, "The request body is too large.")
    raw = await request.body()
    if len(raw) > am.MAX_BODY_BYTES:
        raise HTTPException(413, "The request body is too large.")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise HTTPException(400, {"code": "invalid_json", "message": "The request body is not valid JSON."}) from None
    try:
        return am.parse_request(body, default_max_tokens=gateway.DEFAULT_MAX_TOKENS,
                                default_temperature=gateway.DEFAULT_TEMPERATURE)
    except gateway_request.RequestError as exc:
        raise HTTPException(400, {"code": exc.code, "message": exc.message}) from exc


def _too_long(cause: BaseException | None) -> HTTPException | None:
    found = inference_errors.prompt_overflow(cause)
    if found is None:
        return None
    return HTTPException(400, {"code": "prompt_too_long", "message": am.prompt_too_long(*found)})


def _describe(failure: object, plan: gateway.ChatPlan) -> am.Failure:
    """A failure of the engine, as the status and message to tell the client."""
    if isinstance(failure, dict):
        # This machine's own model: the chunk carries its message and category.
        text = str(failure.get("__error__") or "inference failed")
        found = inference_errors.prompt_overflow(text)
        if found is not None:
            return am.Failure(400, am.prompt_too_long(*found), "prompt_too_long")
        category = failure.get("__category__")
        category = category if isinstance(category, str) else None
        status = 503 if category == inference_errors.GPU_OUT_OF_MEMORY else 500
        return am.Failure(status, text, category)
    exc = gateway.http_failure(failure, plan)  # type: ignore[arg-type]
    message, code = am.error_detail(exc.detail)
    return am.Failure(exc.status_code, message, code)


async def _counted(runtime: Any, text: str) -> int | None:
    """Tokens in ``text`` by this machine's tokenizer, or None when it cannot say
    soon. The tokenizer waits behind a request that is queued for the model, and a
    count is not worth holding a reply up for, so it gets a moment and no more."""
    count = getattr(runtime, "count_tokens", None)
    if count is None:
        return None
    try:
        return await asyncio.wait_for(count(text), COUNT_WAIT_S)
    except asyncio.TimeoutError:
        return None


async def _input_tokens(plan: gateway.ChatPlan, text: str) -> int:
    """Prompt tokens for message_start: this machine's tokenizer for its own model,
    otherwise a length estimate."""
    if not (plan.peer or plan.use_pool):
        counted = await _counted(plan.runtime, text)
        if counted is not None:
            return counted
    return am.estimate_tokens(text)


async def _metered(events: AsyncIterator[str], plan: gateway.ChatPlan) -> AsyncIterator[str]:
    """Count content_block_delta events as output tokens, the way the OpenAI route
    counts chunks, then record one consumed usage row. Only the event name is
    read, never the content. Records in finally so a client disconnect still lands."""
    started = time.monotonic()
    first_ms: int | None = None
    count = 0
    ok = True
    try:
        async for event in events:
            if event.startswith("event: content_block_delta"):
                if first_ms is None:
                    first_ms = int((time.monotonic() - started) * 1000)
                count += 1
            elif event.startswith("event: error"):
                ok = False
            yield event
    finally:
        await gateway.record_usage(
            plan, output_tokens=count, ttft_ms=first_ms,
            duration_ms=int((time.monotonic() - started) * 1000), ok=ok,
        )


async def _create_message(request: Request, x_potluck_scope: str | None, x_potluck_circle: str | None):
    parsed = await _parse(request)
    try:
        plan = await gateway.plan_chat(
            gateway.ChatJob(
                parsed.prepared, parsed.model, parsed.temperature, parsed.max_tokens, parsed.stream,
                stop=parsed.stop or None, stop_when_blocking=True,
            ),
            x_potluck_scope, x_potluck_circle,
        )
    except HTTPException as exc:
        # Which model was asked for: a client such as Claude Code sends requests for
        # more than one model name, and a refusal is hard to place without it.
        logger.info("gateway_messages_refused status=%d model=%s", exc.status_code, am.shown_name(parsed.model))
        raise
    logger.info(
        "gateway_messages stream=%s tools=%d thinking_shown=%s first_call_only=%s",
        parsed.stream, len(parsed.prepared.tools or ()), parsed.show_thinking, parsed.first_call_only,
    )
    text = am.prompt_text(parsed)
    options = am.ReplyOptions(
        model=parsed.model or plan.served_model or "", show_thinking=parsed.show_thinking,
        first_call_only=parsed.first_call_only, input_estimate=am.estimate_tokens(text),
    )
    served_by = {"X-Potluck-Served-By": plan.served_by}

    if not parsed.stream:
        try:
            result = await gateway.complete_chat(plan)
        except HTTPException as exc:
            raise (_too_long(exc.__cause__) or exc) from exc.__cause__
        message, estimated = am.message_from_completion(result, options)
        return JSONResponse(message, headers={**served_by, **({"X-Potluck-Usage": "estimated"} if estimated else {})})

    stream = am.MessageStream(
        gateway.shaped_chunks(plan), options, await _input_tokens(plan, text),
        lambda failure: _describe(failure, plan), ping_interval=PING_INTERVAL_S,
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
            **served_by, "X-Potluck-Usage": "estimated",
        },
    )


@router.post("/messages")
async def create_message(
    request: Request,
    x_potluck_scope: str | None = Header(default=None),
    x_potluck_circle: str | None = Header(default=None),
):
    """Anthropic POST /v1/messages. A query string such as ?beta=true is ignored."""
    try:
        return await _create_message(request, x_potluck_scope, x_potluck_circle)
    except HTTPException as exc:
        return _error_response(exc.status_code, exc.detail, exc.headers)
    except Exception:  # noqa: BLE001
        logger.exception("gateway_messages_failed")
        return _error_response(500, "The engine hit an unexpected error.")


@router.post("/messages/count_tokens")
async def count_message_tokens(request: Request):
    """Anthropic POST /v1/messages/count_tokens: ``{"input_tokens": N}``.

    Counted with the loaded model's tokenizer when the request names that model
    (or none), over the system and message text and the tools as JSON, without the
    chat template's own markup. Any other model, or an engine that cannot tokenize,
    gets a length estimate and X-Potluck-Usage: estimated. No model is loaded and no
    route is chosen."""
    try:
        parsed = await _parse(request)
        text = am.prompt_text(parsed)
        runtime = gateway.get_runtime()
        counted = None
        if parsed.model is None or (runtime.is_loaded() and parsed.model == runtime.current_model_id()):
            counted = await _counted(runtime, text)
        if counted is not None:
            return JSONResponse({"input_tokens": counted})
        return JSONResponse({"input_tokens": am.estimate_tokens(text)}, headers={"X-Potluck-Usage": "estimated"})
    except HTTPException as exc:
        return _error_response(exc.status_code, exc.detail, exc.headers)
    except Exception:  # noqa: BLE001
        logger.exception("gateway_count_tokens_failed")
        return _error_response(500, "The engine hit an unexpected error.")
