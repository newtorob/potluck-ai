"""The circle listener: what other circle members' machines may call.

This is a minimal ASGI app of its own, on a port of its own (services/
circle_listener.py binds it), apart from the engine's main app. No household
route is mounted here, so /peer/*, /health, /v1/* and everything else are 404
by construction, and the main app answers /circle/* with 403 to any caller that
is not on this machine. Exactly two routes exist:

  GET  /circle/capabilities       what this machine can serve right now
  POST /circle/chat/completions   one stateless chat completion, streamed

The wire shapes are pinned to services/coordinator/tests/fixtures/
circle_link_shapes.json (the `engine` section).

Who may call: services/circle_gate.py. The gate runs in front of the routes,
before the request body is read, so a caller who fails it learns nothing about
the machine and cannot make it parse anything.

Privacy invariants, the same as /peer/chat/completions:
  * No memory injection on inference (no `_build_enhanced_messages` call).
  * No conversation persistence (no `chat_conversations` write).
  * No agent observability session (no `_get_chat_session_id` call).
  * No memory extraction trigger (no `_fire_extraction` task).
  * Logs carry counts only: message count, character count, token count,
    duration. Never the text, never a source address above debug level.
  * The one record of a served request is a metadata-only usage row (scope
    circle, tokens, duration), the same row a relayed request leaves.

Capacity: a direct request takes a place under the same `max_concurrent_jobs`
as relay jobs (DirectAdmission), and `contributor_max_tokens` applies.

A request that is running is watched. Every couple of seconds the gate is
asked again whether the link is still there and this machine still shares with
circles; when it is not, or after a hard time limit, the response is cut and
the model is released. The watcher runs next to the response, not inside it,
so a caller that stops reading cannot hold a place forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from potluck_local.services import circle_gate, inference_errors
from potluck_local.services.circle_gate import CircleRoster, DirectAdmission, DirectSlot
from potluck_local.services.model_identity import ModelIdentityError

logger = logging.getLogger(__name__)

CAPABILITIES_PATH = "/circle/capabilities"
CHAT_PATH = "/circle/chat/completions"
_ROUTES = frozenset({("GET", CAPABILITIES_PATH), ("POST", CHAT_PATH)})

# A request body bigger than this is refused before it is parsed. The relay is
# bounded the same way by its own limits; a long chat is a few hundred KB.
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_MESSAGES = 2048
# Parity with the coordinator's SamplingParams.max_tokens ceiling.
MAX_OUTPUT_TOKENS = 4096

# A caller has this long to send its whole request. The gate has already vouched
# for it, but a connection that trickles a body must not be held open for ever.
BODY_TIMEOUT_S = 30.0
# How often a running request is checked against the gate again.
RECHECK_INTERVAL_S = 2.0
# A sidecar that cannot be read this many checks in a row ends the request.
UNREADABLE_CHECKS_ALLOWED = 3
# One request never runs longer than this (the household peer path's limit).
REQUEST_CEILING_S = 240.0

# The model's reply text is never inspected or kept; only these are counted.
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class CircleMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class CircleChatRequest(BaseModel):
    """The household peer chat body without potluck_batch and potluck_model_sha256.
    Unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")

    messages: list[CircleMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    model: str = Field(min_length=1, max_length=256)
    stream: Literal[True] = True
    # Strict: a number is a number, not text that reads as one.
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, strict=True)
    max_tokens: int = Field(default=2048, ge=1, le=MAX_OUTPUT_TOKENS, strict=True)
    stop: list[str] | None = Field(default=None, max_length=8)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status, headers=headers)


def _bad_token() -> JSONResponse:
    return _error(401, "bad_token", "unauthorized")


def _not_a_circle_peer() -> JSONResponse:
    return _error(403, "not_a_circle_peer", "forbidden")


def _not_sharing() -> JSONResponse:
    return _error(503, "not_sharing", "not sharing with circles")


def _no_model() -> JSONResponse:
    return _error(503, "no_model", "no model loaded")


def _model_mismatch() -> JSONResponse:
    return _error(409, "model_mismatch", "model not loaded")


def _busy() -> JSONResponse:
    return _error(429, "busy", "busy", {"Retry-After": "2"})


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


_DONE = "data: [DONE]\n\n"


def _error_event(category: str, message: str) -> str:
    return _sse({"error": {"category": category, "message": message}})


# ---------------------------------------------------------------------------
# The gate, in front of the routes
# ---------------------------------------------------------------------------


@dataclass
class CircleDeps:
    """Everything the circle app needs from the engine. Injected, so the app has
    no import of the contributor service and tests can stand in for each part."""

    roster: CircleRoster
    admission: DirectAdmission
    # None while this machine shares with circles and policy allows serving,
    # else a short reason code.
    sharing_refusal: Callable[[], str | None]
    # network.contributor_max_tokens, read live.
    max_tokens_limit: Callable[[], int | None]
    runtime: Callable[[], Any]


@dataclass(frozen=True)
class CircleCaller:
    """A caller that passed the gate: the two socket addresses it was judged on."""

    client_host: str
    server_host: str


def _addresses(scope: Scope) -> tuple[Any, Any]:
    client = scope.get("client")
    server = scope.get("server")
    client_host = client[0] if isinstance(client, (tuple, list)) and len(client) == 2 else None
    server_host = server[0] if isinstance(server, (tuple, list)) and len(server) == 2 else None
    return client_host, server_host


def _authorization(scope: Scope) -> str | None:
    for name, value in scope.get("headers", ()):
        if name == b"authorization":
            return value.decode("latin-1")
    return None


class CircleGateMiddleware:
    """Pure ASGI, so nothing (no body parsing, no framework routing) runs before it."""

    def __init__(self, app: ASGIApp, deps: CircleDeps) -> None:
        self.app = app
        self.deps = deps

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        if (scope["method"], scope["path"]) not in _ROUTES:
            await _not_found()(scope, receive, send)
            return
        client_host, server_host = _addresses(scope)
        token = circle_gate.bearer_token(_authorization(scope))
        reason = await self.deps.roster.refusal_reason(client_host, server_host, token)
        if reason is not None:
            # A reason code and the source, at debug level, so a stranger cannot fill the log.
            logger.debug("circle_refused reason=%s source=%s", reason, client_host)
            refusal = _bad_token() if circle_gate.is_token_reason(reason) else _not_a_circle_peer()
            await refusal(scope, receive, send)
            return
        not_sharing = self.deps.sharing_refusal()
        if not_sharing is not None:
            logger.debug("circle_refused reason=%s source=%s", not_sharing, client_host)
            await _not_sharing()(scope, receive, send)
            return
        scope["circle_caller"] = CircleCaller(client_host=str(client_host), server_host=str(server_host))
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _deps(request: Request) -> CircleDeps:
    return request.app.state.deps


async def capabilities(request: Request) -> Response:
    """What this machine can serve right now. No hostname, hardware, rpc endpoint
    or speed: a circle member learns only what it needs to pick this machine."""
    deps = _deps(request)
    runtime = deps.runtime()
    loaded = runtime.current_model_id() if runtime.is_loaded() else None
    in_flight, cap = deps.admission.snapshot()
    return JSONResponse(
        {
            "schemaVersion": 1,
            "protocol_version": 1,
            "available_for_inference": loaded is not None,
            "loaded_model": loaded,
            "busy": cap is not None and in_flight >= cap,
            "in_flight": in_flight,
            "max_concurrent_jobs": cap,
        }
    )


async def _read_body(request: Request) -> bytes | None:
    """The request body, or None when it is bigger than MAX_BODY_BYTES."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    parts: list[bytes] = []
    size = 0
    async for part in request.stream():
        size += len(part)
        if size > MAX_BODY_BYTES:
            return None
        parts.append(part)
    return b"".join(parts)


async def chat_completions(request: Request) -> Response:
    """One stateless chat completion for a circle member. The member's machine
    gets none of the request, none of the reply and none of the inferred
    memories from this call: the request hits the runtime, the runtime streams
    back, and that is all."""
    deps = _deps(request)
    caller: CircleCaller = request.scope["circle_caller"]

    try:
        async with asyncio.timeout(BODY_TIMEOUT_S):
            raw = await _read_body(request)
    except TimeoutError:
        return _error(408, "request_timeout", "request took too long")
    if raw is None:
        return _error(413, "request_too_large", "request body is too large")
    try:
        body = CircleChatRequest.model_validate_json(raw)
    except ValidationError:
        # No detail: the input would be echoed back, and a gate-passing caller
        # already knows what it sent.
        return _error(422, "invalid_request", "invalid request")

    runtime = deps.runtime()
    if not runtime.is_loaded():
        return _no_model()
    loaded = runtime.current_model_id()
    if body.model != loaded:
        return _model_mismatch()
    limit = deps.max_tokens_limit()
    if limit is not None and body.max_tokens > limit:
        return _error(422, "max_tokens_over_limit", "max_tokens is over this machine's limit")
    slot = deps.admission.try_acquire()
    if slot is None:
        return _busy()

    try:
        # Counts only. The text never reaches a log call.
        message_count = len(body.messages)
        total_chars = sum(len(m.content) for m in body.messages)
        logger.info(
            "circle_chat_request message_count=%d total_chars=%d max_tokens=%d",
            message_count, total_chars, body.max_tokens,
        )
        return GuardedStream(
            _stream_reply(runtime, body, str(loaded)),
            slot=slot,
            guard=_guard(deps, caller),
        )
    except BaseException:
        slot.release()
        raise


async def _stream_reply(runtime: Any, body: CircleChatRequest, loaded: str) -> AsyncGenerator[str, None]:
    """The reply as SSE: content chunks, one meta line, [DONE]. Failures are
    reported as a generic error event; the real reason stays in this process."""
    started = time.monotonic()
    count = 0
    try:
        source = runtime.stream_chat_raw(
            [m.model_dump() for m in body.messages],
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            stop=body.stop,
            expected_model=loaded,
        )
        async with aclosing(source):
            async for chunk in source:
                if isinstance(chunk, dict) and "__error__" in chunk:
                    fixed = inference_errors.other_machine_error(chunk.get("__category__"))
                    yield _error_event(fixed["category"], fixed["message"])
                    yield _DONE
                    return
                try:
                    delta = chunk["choices"][0].get("delta", {}).get("content")
                except (KeyError, IndexError, AttributeError, TypeError):
                    delta = None
                if not delta:
                    continue
                count += 1
                yield _sse({"choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]})
    except ModelIdentityError:
        yield _error_event("model_mismatch", "Model changed")
        yield _DONE
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("circle_chat_failed error=%s", type(exc).__name__)
        yield _error_event("inference_error", "Inference failed")
        yield _DONE
        return
    duration_ms = int((time.monotonic() - started) * 1000)
    finish_reason = "length" if count >= body.max_tokens else "stop"
    logger.info(
        "circle_chat_complete output_tokens=%d duration_ms=%d reason=%s", count, duration_ms, finish_reason
    )
    await _record_served(loaded, count, duration_ms)
    yield _sse(
        {
            "meta": {
                "served_by": "contributor",
                "served_scope": "circle",
                "output_tokens": count,
                "duration_ms": duration_ms,
                "finish_reason": finish_reason,
            }
        }
    )
    yield _DONE


async def _record_served(model_slug: str, output_tokens: int, duration_ms: int) -> None:
    """One metadata-only usage row, the same a relayed job leaves (scope circle).
    Never the text and never who asked."""
    try:
        from potluck_local.services.usage_store import record_usage_event

        await record_usage_event(
            direction="served",
            scope="circle",
            model_slug=model_slug,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            ok=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug("usage_record_served_circle_failed", exc_info=True)


def _guard(deps: CircleDeps, caller: CircleCaller) -> Callable[[], Awaitable[str]]:
    """The watcher for one running request: returns the reason the request must
    stop. Runs until then."""

    async def watch() -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REQUEST_CEILING_S
        unreadable = 0
        while True:
            await asyncio.sleep(max(0.0, min(RECHECK_INTERVAL_S, deadline - loop.time())))
            if loop.time() >= deadline:
                return "time_limit"
            if deps.sharing_refusal() is not None:
                return "not_sharing"
            state = await deps.roster.link_state(caller.client_host, caller.server_host)
            if state == circle_gate.GONE:
                return "link_gone"
            unreadable = unreadable + 1 if state == circle_gate.UNKNOWN else 0
            if unreadable >= UNREADABLE_CHECKS_ALLOWED:
                return "link_unreadable"

    return watch


class GuardedStream(StreamingResponse):
    """A streaming response that stops when its watcher says so, and always gives
    its job slot back.

    The watcher runs beside the response. A caller that never reads makes the
    send block, and a stalled model makes the generator wait; neither stops the
    watcher, which cancels the response and closes the generator.
    """

    def __init__(
        self,
        content: AsyncGenerator[str, None],
        *,
        slot: DirectSlot,
        guard: Callable[[], Awaitable[str]],
    ) -> None:
        super().__init__(content, media_type="text/event-stream", headers=_SSE_HEADERS)
        self._slot = slot
        self._guard = guard
        self._content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        sent = {"started": False, "finished": False}

        async def tracked_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                sent["started"] = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                sent["finished"] = True
            await send(message)

        inner = asyncio.ensure_future(StreamingResponse.__call__(self, scope, receive, tracked_send))
        watcher = asyncio.ensure_future(self._guard())
        try:
            await asyncio.wait({inner, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if inner.done():
                inner.result()
                return
            try:
                reason = watcher.result()
            except Exception:  # noqa: BLE001 - a watcher that fails cannot vouch for the link
                reason = "watcher_failed"
            logger.info("circle_chat_cut reason=%s", reason)
            inner.cancel()
            await asyncio.gather(inner, return_exceptions=True)
            await self._finish_cut(send, sent)
        except asyncio.CancelledError:
            inner.cancel()
            await asyncio.gather(inner, return_exceptions=True)
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if not inner.done():  # only if something above went wrong: never leave it running
                inner.cancel()
                await asyncio.gather(inner, return_exceptions=True)
            try:
                # Runs the generator's own cleanup, which hands the model back.
                await self._content.aclose()
            finally:
                self._slot.release()

    async def _finish_cut(self, send: Send, sent: dict[str, bool]) -> None:
        """Tell the caller why the stream ended, if it can still be told."""
        try:
            async with asyncio.timeout(1.0):
                if not sent["started"]:
                    await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
                if not sent["finished"]:
                    body = _error_event("link_ended", "The link ended.") + _DONE
                    await send({"type": "http.response.body", "body": body.encode(), "more_body": False})
        except Exception:  # noqa: BLE001 - the caller may be gone; the cut is done either way
            pass


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_circle_app(deps: CircleDeps) -> ASGIApp:
    """The circle listener's app: the gate in front of two routes, nothing else."""
    app = Starlette(
        routes=[
            Route(CAPABILITIES_PATH, capabilities, methods=["GET"]),
            Route(CHAT_PATH, chat_completions, methods=["POST"]),
        ],
        # No docs, no OpenAPI, no CORS, no default error pages beyond Starlette's own.
    )
    app.state.deps = deps
    return CircleGateMiddleware(app, deps)
