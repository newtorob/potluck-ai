"""Borrowing over a direct circle link, with the coordinator's relay as the fallback.

A circle request used to go through the coordinator (pool_routing's
/jobs/stream). Now it tries a direct link first: the coordinator picks a lender
in the circle and tells both machines' mesh sidecars to add the other as a
restricted WireGuard peer, then this engine calls the lender's circle listener
(routers/circle_link.py) machine to machine. The open pool is not involved and
never will be: a circle request is never sent to the pool.

The algorithm, for one request:

  1. Direct is tried only when the owner has not turned it off, the request fits
     what a lender accepts, the coordinator has not said links are disabled in
     the last five minutes (or answered the link route with a bare 404 or 405,
     which only a coordinator without the route does), and the local mesh sidecar
     advertises circle_link_v1. Otherwise the relay is used at once, exactly as
     before links existed.
  2. A cached live link for the same circle and model is reused when it has at
     least 60 seconds left. Otherwise one is created (POST .../links). Any answer
     but 201 means the relay, at once. Two answers are remembered for five
     minutes, for every circle on that coordinator, so that later requests do not
     pay for the call: 503 links_disabled, and a 404 or 405 that carries no error
     code (the coordinator's own 404s for this route always carry one, so a bare
     one means the route does not exist).
  3. The link must show up in the sidecar's /circle-peers as outbound and
     reachable. Up to 6 seconds in all (the create call included) are spent
     waiting for that. Past that, the link is ended and the relay is used.
  4. POST /circle/chat/completions to the lender over the mesh: 3 second connect
     timeout, and the first-token budget the coordinator gives a relay peer, so
     both paths give up alike.
  5. A failure before anything was yielded tries one more lender (the failed link
     is named in exclude_link_ids), then the relay. After the first token the
     request is committed: a failure is an error, never a replay.
  5b. When direct cannot work for a circle (a link did not become usable inside
     the budget, or two lenders in a row failed before the first token), direct
     is skipped for that circle for two minutes and its requests go straight to
     the relay, so a pair that cannot connect does not pay the wait every time.
     A direct request that succeeds clears it.
  6. While a link is in use it is renewed at renew_after_s. A 410 on renew is
     final; a 429 waits for Retry-After. Renewing stops when the link has been
     idle for two minutes, and the link lapses by itself.

What this engine will and will not trust:
  * Where the prompt is sent is decided by the local mesh sidecar, not by the
    coordinator's answer alone. The request goes only to an address the sidecar
    lists as an outbound circle peer for that very link, and only if it is in the
    mesh range. Nothing is sent to an address the coordinator named but the
    sidecar did not confirm.
  * The lender's stream is parsed with the same checks as the relay's
    (remote_stream.openai_chunks). The receipt (`potluck` on the last chunk) is
    built here: only the numeric fields and finish reason come from the lender;
    the path and transport are this engine's own.
  * The link token lives in memory only: never logged, never in a repr.

The shapes are pinned to services/coordinator/tests/fixtures/
circle_link_shapes.json (`http`, `engine` and `receipt`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import circle_mesh, pool_routing
from .peer_trust import parse_ip
from .remote_stream import RemoteStreamError, openai_chunks

logger = logging.getLogger(__name__)

# ---- the numbers (all from the design brief) --------------------------------

# Up to this long, in all, for a new link to be usable: the create call and the
# wait for the sidecar to show it outbound and reachable.
LINK_READY_BUDGET_S = 6.0
LINK_POLL_INTERVAL_S = 0.15
# A cached link is reused only with at least this much life left.
MIN_REUSE_REMAINING_S = 60.0
# A link is kept alive only while a request is active or one started this recently.
RENEW_ACTIVE_WINDOW_S = 120.0
# A coordinator that said links are disabled (503 links_disabled), or that has no
# link route at all (a 404 or 405 with no error code), is not asked again for this
# long. The memory is per coordinator, not per circle.
LINKS_DISABLED_MEMORY_S = 300.0
CONNECT_TIMEOUT_S = 3.0
# A direct attempt is made at most this many times (the first lender and one other).
MAX_DIRECT_ATTEMPTS = 2

# The first-token budget is the coordinator's own (routers/jobs.py): a floor, plus
# the prompt's estimated size at the tier proxy's speed, with a ceiling. A test
# reads those constants and fails if these drift.
FIRST_TOKEN_FLOOR_S = 5.0
FIRST_TOKEN_MAX_S = 90.0
PROMPT_TOKENS_PER_SECOND = 25.0
CHARS_PER_TOKEN = 4.0

# After the first token a stream may be quiet this long, and one request runs no
# longer than this: the household peer path's own limits.
IDLE_TIMEOUT_S = 30.0
REQUEST_TIMEOUT_S = 240.0

# After direct failed for a circle in one of the two ways above, it is not tried
# for that circle for this long. POTLUCK_CIRCLE_COOLDOWN_S overrides it (0 turns
# the cooldown off); tests and live runs shorten it that way.
COOLDOWN_S = 120.0
COOLDOWN_ENV = "POTLUCK_CIRCLE_COOLDOWN_S"

RENEW_TIMEOUT_S = 5.0
RENEW_RETRY_S = 10.0
DELETE_TIMEOUT_S = 3.0

# What a lender's circle listener accepts (routers/circle_link.py). Kept in step
# by a test; a request outside it goes to the relay without a direct attempt.
CHAT_PATH = "/circle/chat/completions"
_ROLES = frozenset({"system", "user", "assistant"})
_MAX_MESSAGES = 2048
_MAX_BODY_BYTES = 4 * 1024 * 1024
_MAX_OUTPUT_TOKENS = 4096

# A lender's whole answer is a few hundred KB at most (4096 tokens). A stream
# bigger than this cannot be one, whatever it claims to be.
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

_CIRCLE_SCOPE = "circle"

# ---- the coordinator's answers ----------------------------------------------


class _Lender(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mesh_ip: str
    port: int = Field(ge=1, le=65535)


class LinkCreated(BaseModel):
    """201 from POST /me/circles/{circle_id}/links. Unknown keys are ignored so a
    newer coordinator can add some."""

    model_config = ConfigDict(extra="ignore")

    link_id: uuid.UUID
    circle_id: uuid.UUID
    model_slug: str
    # Goes into an Authorization header: URL-safe base64 text only.
    token: str = Field(min_length=16, max_length=512, pattern=r"^[A-Za-z0-9_\-=.~+/]+$")
    lender: _Lender
    ttl_s: int = Field(ge=1, le=86400)
    renew_after_s: int = Field(ge=1, le=86400)
    expires_at: str
    ready: bool


class LinkRenewed(BaseModel):
    """200 from POST .../links/{link_id}/renew."""

    model_config = ConfigDict(extra="ignore")

    link_id: uuid.UUID
    ttl_s: int = Field(ge=1, le=86400)
    renew_after_s: int = Field(ge=1, le=86400)
    expires_at: str
    delivered: dict[str, bool]


class DirectUnavailable(Exception):
    """No link could be made for this request. The relay is used. `cooldown` is
    set when the cause says direct cannot work for this circle for a while (a
    link that did not become usable), as opposed to a refusal worth asking again."""

    def __init__(self, reason: str, *, cooldown: bool = False) -> None:
        self.reason = reason
        self.cooldown = cooldown
        super().__init__(reason)


class DirectFailed(Exception):
    """A link was made but the lender gave no answer, before anything was yielded.
    Another lender is tried, then the relay."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# ---- logging ------------------------------------------------------------------


class _HideLenderRequests(logging.Filter):
    """httpx logs every request's URL at INFO. A request to a circle member's
    machine names that machine's mesh address, which nothing needs in a log, so
    those lines (and only those) are dropped."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (isinstance(args, tuple) and len(args) > 1 and CHAT_PATH in str(args[1]))


def _hide_lender_requests() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, _HideLenderRequests) for f in httpx_logger.filters):
        httpx_logger.addFilter(_HideLenderRequests())


_hide_lender_requests()

# ---- state (per process) ----------------------------------------------------


@dataclass(eq=False)
class Link:
    circle_id: str
    model: str
    link_id: str
    token: str = field(repr=False)  # in memory only; never logged
    lender_ip: str
    port: int
    base: str
    # _now() readings. `deadline` is when the link ends unless renewed.
    deadline: float
    renew_after_s: int
    next_renew_at: float
    last_used: float
    active: int = 0
    ended: bool = False
    renewer: "asyncio.Future[None] | None" = None


@dataclass(frozen=True)
class Plan:
    base: str
    node_id: str
    circle_id: str
    model: str
    body: bytes = field(repr=False)  # the request, prompt and all: never in a repr
    first_token_budget: float
    # The coordinator said links are off: go straight to the relay, but still
    # report the relay as the path taken.
    skip_direct: bool = False


_now: Callable[[], float] = time.monotonic  # replaced in tests
_links: dict[tuple[str, str], Link] = {}
_acquiring: dict[tuple[str, str], "asyncio.Future[tuple[Link, str | None]]"] = {}
_disabled_until: dict[str, float] = {}
_cooldown_until: dict[str, float] = {}  # circle id -> _now() until which direct is skipped
_background: set["asyncio.Future[Any]"] = set()


def reset() -> None:
    """Forget every link, pending call and memory. For tests."""
    pending = [link.renewer for link in _links.values() if link.renewer is not None]
    pending += list(_acquiring.values()) + list(_background)
    for task in pending:
        try:
            task.cancel()
        except RuntimeError:  # its loop is already closed
            pass
    _links.clear()
    _acquiring.clear()
    _disabled_until.clear()
    _cooldown_until.clear()
    _background.clear()


def _new_client(**kwargs: Any) -> httpx.AsyncClient:
    """The one place an HTTP client is made, so tests can stand in for the network."""
    return httpx.AsyncClient(**kwargs)


def cooldown_seconds() -> float:
    raw = os.environ.get(COOLDOWN_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return COOLDOWN_S
        if math.isfinite(value) and value >= 0:
            return value
    return COOLDOWN_S


def _start_cooldown(circle_id: str, reason: str) -> None:
    seconds = cooldown_seconds()
    if seconds > 0:
        _cooldown_until[circle_id] = _now() + seconds
        logger.info("circle_direct_cooldown seconds=%d reason=%s", int(seconds), reason)


def _cooling_down(circle_id: str) -> bool:
    until = _cooldown_until.get(circle_id)
    if until is None:
        return False
    if _now() >= until:
        del _cooldown_until[circle_id]
        return False
    return True


def direct_enabled() -> bool:
    """The owner's setting for direct circle links (default on). If the setting
    cannot be read, links are off, which is today's behaviour."""
    try:
        from potluck_local.services.settings_manager import get_settings

        return bool(get_settings().network.direct_circle_links)
    except Exception:  # noqa: BLE001
        logger.debug("direct_circle_links_setting_unavailable", exc_info=True)
        return False


def first_token_budget(messages: list[dict]) -> float:
    """How long a lender gets to produce its first token: what the coordinator
    gives a relay peer for the same request (routers/jobs.py::_first_token_deadline,
    with its tier proxy for a peer whose speed it has not measured)."""
    chars = len(pool_routing.last_user_prompt(messages)) + sum(len(m.get("content", "")) for m in messages)
    derived = FIRST_TOKEN_FLOOR_S + (chars / CHARS_PER_TOKEN) / PROMPT_TOKENS_PER_SECOND
    return max(FIRST_TOKEN_FLOOR_S, min(derived, FIRST_TOKEN_MAX_S))


def chat_body(messages: list[dict], model: str, temperature: float, max_tokens: int) -> dict:
    """POST /circle/chat/completions body: the fixture's engine.chat_request."""
    return {
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        "model": model,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stop": None,
    }


def _fits_a_lender(messages: Any, model: Any, temperature: Any, max_tokens: Any) -> bytes | None:
    """The encoded body when a lender's listener would accept this request, else
    None (the request then goes to the relay without a direct attempt)."""
    if not isinstance(model, str) or not 1 <= len(model) <= 256:
        return None
    if not isinstance(messages, list) or not 1 <= len(messages) <= _MAX_MESSAGES:
        return None
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in _ROLES or not isinstance(m.get("content"), str):
            return None
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        return None
    if not math.isfinite(temperature) or not 0.0 <= temperature <= 2.0:
        return None
    if type(max_tokens) is not int or not 1 <= max_tokens <= _MAX_OUTPUT_TOKENS:
        return None
    body = json.dumps(chat_body(messages, model, temperature, max_tokens), separators=(",", ":")).encode()
    return body if len(body) <= _MAX_BODY_BYTES else None


# ---- what a lender may say ---------------------------------------------------

# A failure reported by a lender's stream is described here in this engine's own
# words, chosen by a category from this fixed set. What the lender wrote (its
# category, its message) never reaches a log line or the caller: it is another
# person's machine, possibly a modified build. remote_stream raises the same
# exception type for its own checks and for an error event from the stream, and
# the two cannot be told apart cleanly, so every one is treated as the lender's.
ERROR_TEXT: dict[str, str] = {
    "inference_error": "The circle member's computer could not finish the reply.",
    "model_mismatch": "The circle member's computer no longer has that model loaded.",
    "link_ended": "The link to the circle member's computer ended.",
    "stream_protocol_error": "The circle member's computer sent a reply this app could not read.",
    "stream_interrupted": "The connection to the circle member's computer ended before the reply finished.",
    "stream_timeout": "The circle member's computer stopped answering.",
    "peer_unreachable": "The connection to the circle member's computer failed.",
}


def fixed_category(value: object) -> str:
    """A category from ERROR_TEXT. Anything else, whatever it is, is inference_error."""
    return value if isinstance(value, str) and len(value) <= 32 and value in ERROR_TEXT else "inference_error"


# ---- what a lender's chunks may carry ----------------------------------------------

# A lender decides what is in its stream, and a client of the gateway reads every
# key of every chunk we pass on (a tool call, a function call, a refusal, a second
# choice). The relay cannot say any of that, since the coordinator builds its
# chunks. So a chunk from a lender is rebuilt here from this engine's own
# template: one choice, a delta of at most `role` ("assistant" only) and `content`
# (a non-empty string only), and a finish reason from the set below. Everything
# else the lender sent is dropped, at the delta, choice and chunk level alike.
# `tool_calls` and `function_call` finish reasons become "stop": their payload is
# dropped, and a client told to run calls that are not there would misbehave.
_FINISH_REASONS = {"stop": "stop", "length": "length", "content_filter": "content_filter",
                   "tool_calls": "stop", "function_call": "stop"}
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _stream_template(model: str) -> dict[str, Any]:
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model}


def _rebuilt(template: dict[str, Any], chunk: dict) -> tuple[dict, bool, bool]:
    """(the chunk to pass on, whether it carries output, whether it is the last).
    Output means non-empty string content. A chunk whose only substance was in a
    key that was dropped carries none."""
    choice = chunk["choices"][0]
    delta: dict[str, str] = {}
    source = choice.get("delta")
    if isinstance(source, dict):
        if source.get("role") == "assistant":
            delta["role"] = "assistant"
        text = source.get("content")
        if isinstance(text, str) and text:
            delta["content"] = text
    raw_reason = choice.get("finish_reason")
    finished = raw_reason is not None
    reason = (_FINISH_REASONS.get(raw_reason) or "stop") if finished and isinstance(raw_reason, str) else None
    clean: dict[str, Any] = {**template, "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
    if finished:
        usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else {}
        clean["usage"] = {k: usage[k] if type(usage.get(k)) is int and usage[k] >= 0 else None for k in _USAGE_KEYS}
        meta = chunk.get("potluck")
        if isinstance(meta, dict):
            clean["potluck"] = {"finish_reason": reason, **{k: meta[k] for k in ("output_tokens", "duration_ms") if k in meta}}
    return clean, "content" in delta, finished


# ---- the receipt ------------------------------------------------------------


def receipt(meta: dict | None, *, path: str, transport: str | None, scope: str = _CIRCLE_SCOPE) -> dict:
    """The terminal `meta` of a circle request, built here. From the stream come
    only the numeric fields and the finish reason (remote_stream has already
    checked their types); everything else is this engine's own statement."""
    meta = meta or {}
    out: dict[str, Any] = {"served_by": "contributor", "served_scope": scope, "path": path, "transport": transport}
    for key in ("output_tokens", "duration_ms"):
        if key in meta:
            out[key] = meta[key]
    if "finish_reason" in meta:
        out["finish_reason"] = meta["finish_reason"]
    return out


def transport_of(mode: str | None) -> str | None:
    """The receipt's transport from the sidecar's reachability_mode."""
    return {"direct": "udp", "derp": "derp"}.get(mode or "")


def _with_receipt(chunk: dict, **kw: Any) -> dict:
    out = dict(chunk)
    out["potluck"] = receipt(chunk.get("potluck"), **kw)
    return out


def _stamp_relay(chunk: dict) -> dict:
    """The relay's terminal chunk with the receipt this engine owes: path relay."""
    if "potluck" not in chunk:
        return chunk
    scope = chunk["potluck"].get("served_scope", _CIRCLE_SCOPE)
    return _with_receipt(chunk, path="relay", transport=None, scope=scope)


# ---- reading the coordinator's errors ---------------------------------------


# An error code is a short snake_case word. Anything else is read as no code, so
# nothing a remote machine or a proxy wrote can reach a log line through one.
_CODE = re.compile(r"[a-z0-9_]{1,64}")


def _clean_code(code: object) -> str | None:
    return code if isinstance(code, str) and _CODE.fullmatch(code) else None


def _detail_code(response: httpx.Response) -> str | None:
    """detail.code of a coordinator error body. (PoolDispatchError flattens a
    dict detail to text, so it cannot be used to branch on a code.)"""
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    return _clean_code(detail.get("code") if isinstance(detail, dict) else None)


def _retry_after(response: httpx.Response, default: float = 5.0) -> float:
    raw = response.headers.get("retry-after", "")
    try:
        seconds = float(raw)
    except ValueError:
        seconds = default
    return min(max(seconds, 1.0), 120.0) if math.isfinite(seconds) else default


# ---- the entry point --------------------------------------------------------


async def stream_circle_openai(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
    circle_id: str,
    *,
    relay: Callable[[], AsyncIterator[dict]],
) -> AsyncIterator[dict]:
    """Yield OpenAI chat.completion.chunk dicts for one circle request.

    `relay()` is the coordinator relay (pool_routing's /jobs/stream with scope
    circle) and the only fallback: there is no other place a circle request can go.
    """
    plan = await _plan(messages, model, temperature, max_tokens, circle_id)
    if plan is None:
        async with aclosing(relay()) as chunks:
            async for chunk in chunks:
                yield chunk
        return
    if not plan.skip_direct:
        excluded: list[str] = []
        for _ in range(MAX_DIRECT_ATTEMPTS):
            try:
                link, transport = await _acquire(plan, excluded)
            except DirectUnavailable as exc:
                logger.info("circle_direct_unavailable reason=%s", exc.reason)
                if exc.cooldown:
                    _start_cooldown(plan.circle_id, exc.reason)
                break
            try:
                async with aclosing(_over_link(plan, link, transport)) as chunks:
                    async for chunk in chunks:
                        yield chunk
                _cooldown_until.pop(plan.circle_id, None)  # it worked: direct is fine for this circle
                return
            except DirectFailed as exc:
                logger.info("circle_direct_failed reason=%s", exc.reason)
                _forget(link)
                excluded.append(link.link_id)
        else:
            _start_cooldown(plan.circle_id, "two_lenders_failed")
    async with aclosing(relay()) as chunks:
        async for chunk in chunks:
            yield _stamp_relay(chunk)


async def _plan(
    messages: list[dict], model: str, temperature: float, max_tokens: int, circle_id: str
) -> Plan | None:
    """None when direct is not to be tried at all and the relay is to be used as
    it always was; otherwise how to go about it."""
    if not direct_enabled():
        return None
    body = _fits_a_lender(messages, model, temperature, max_tokens)
    if body is None:
        return None
    try:
        # The id goes into a URL path on the coordinator: only a real UUID may.
        circle_id = str(uuid.UUID(circle_id))
    except (ValueError, AttributeError, TypeError):
        return None
    base = pool_routing.coordinator_http_base()
    status = await circle_mesh.read_status()
    if not base or status is None or not status.supports_links or status.node_id is None:
        return None
    return Plan(
        base=base,
        node_id=status.node_id,
        circle_id=circle_id,
        model=model,
        body=body,
        first_token_budget=first_token_budget(messages),
        skip_direct=_disabled_until.get(base, 0.0) > _now() or _cooling_down(circle_id),
    )


# ---- getting a usable link --------------------------------------------------


async def _acquire(plan: Plan, excluded: list[str]) -> tuple[Link, str | None]:
    """A usable link and its transport, or DirectUnavailable."""
    key = (plan.circle_id, plan.model)
    if excluded:
        # A retry after a failure must not share another request's creation.
        return await _create_and_ready(plan, excluded)
    link = _cached(key)
    if link is not None:
        entry, verdict = await _wait_ready(link, asyncio.get_running_loop().time() + LINK_READY_BUDGET_S, cached=True)
        if entry is not None:
            return link, transport_of(entry.mode)
        if verdict == "unreadable":
            raise DirectUnavailable("sidecar_unreadable")  # the link may be fine; not a reason to wait
        if verdict == "timeout":
            _end_in_background(link)
            raise DirectUnavailable("cached_link_not_ready", cooldown=True)
        _forget(link)  # stale: the sidecar no longer holds this link
    shared = _acquiring.get(key)
    if shared is None:
        shared = asyncio.ensure_future(_create_and_ready(plan, []))
        _acquiring[key] = shared
        shared.add_done_callback(lambda done, k=key: _creation_done(k, done))
    # Requests that arrive while a link is being made wait for it instead of
    # making another, which would replace it (one live link per pair of machines).
    return await asyncio.shield(shared)


def _creation_done(key: tuple[str, str], done: "asyncio.Future[Any]") -> None:
    if _acquiring.get(key) is done:
        del _acquiring[key]
    if not done.cancelled():
        done.exception()  # consumed, so a creation nobody waited for is not reported as lost


async def _create_and_ready(plan: Plan, excluded: list[str]) -> tuple[Link, str | None]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LINK_READY_BUDGET_S
    link = await _create(plan, excluded, deadline)
    try:
        entry, _ = await _wait_ready(link, deadline, cached=False)
    except BaseException:
        _end_in_background(link)
        raise
    if entry is None:
        _end_in_background(link)
        raise DirectUnavailable("link_not_ready", cooldown=True)
    _remember(link)
    return link, transport_of(entry.mode)


def create_body(node_id: str, model: str, excluded: list[str]) -> dict[str, Any]:
    """POST .../links body: the fixture's http.create_request, or its
    create_request_minimal when no link has failed."""
    body: dict[str, Any] = {"requester_node_id": node_id, "model_slug": model}
    if excluded:
        body["exclude_link_ids"] = list(excluded)
    return body


async def _create(plan: Plan, excluded: list[str], deadline: float) -> Link:
    """POST .../links. Any answer but a good 201 is DirectUnavailable."""
    loop = asyncio.get_running_loop()
    body = create_body(plan.node_id, plan.model, excluded)
    headers = {**pool_routing._headers(plan.base), "Accept": "application/json"}
    sent_at = _now()
    remaining = max(deadline - loop.time(), 0.1)
    try:
        async with asyncio.timeout(remaining):
            async with _new_client(timeout=httpx.Timeout(remaining, connect=min(CONNECT_TIMEOUT_S, remaining))) as client:
                response = await client.post(
                    f"{plan.base}/me/circles/{plan.circle_id}/links", json=body, headers=headers
                )
    except (httpx.HTTPError, OSError, TimeoutError):
        raise DirectUnavailable("create_unreachable") from None
    if response.status_code != 201:
        code = _detail_code(response)
        if (response.status_code == 503 and code == "links_disabled") or (
            response.status_code in (404, 405) and code is None
        ):
            # Links are off, or this coordinator has no link route (its own 404s for
            # the route always carry a code). Either way, do not ask again soon.
            _disabled_until[plan.base] = _now() + LINKS_DISABLED_MEMORY_S
        raise DirectUnavailable(f"create_{response.status_code}_{code or 'none'}")
    try:
        created = LinkCreated.model_validate_json(response.content)
    except ValidationError:
        raise DirectUnavailable("create_bad_response") from None
    lender_ip = parse_ip(created.lender.mesh_ip)
    if (
        created.ready is not True
        or str(created.circle_id) != plan.circle_id.lower()
        or created.model_slug != plan.model
        or lender_ip is None
        or lender_ip.version != 4
        or not circle_mesh.in_mesh(lender_ip)
    ):
        raise DirectUnavailable("create_unacceptable")
    return Link(
        circle_id=plan.circle_id.lower(),
        model=plan.model,
        link_id=str(created.link_id),
        token=created.token,
        lender_ip=str(lender_ip),
        port=created.lender.port,
        base=plan.base,
        deadline=sent_at + created.ttl_s,
        renew_after_s=created.renew_after_s,
        next_renew_at=sent_at + created.renew_after_s,
        last_used=_now(),
    )


async def _wait_ready(
    link: Link, deadline: float, *, cached: bool
) -> tuple[circle_mesh.CirclePeer | None, str]:
    """Poll the sidecar until it shows this very link as outbound and reachable.

    (entry, "ready"), or (None, "stale") for a cached link the sidecar no longer
    holds, or (None, "unreadable") for a cached link when the sidecar cannot be
    read, or (None, "timeout"). The entry must name this link's id and port: a
    sidecar entry for the same lender under another link is not this link.
    """
    loop = asyncio.get_running_loop()
    lender_ip = parse_ip(link.lender_ip)
    while True:
        peers = await circle_mesh.read_circle_peers()
        if peers is not None:
            mine = next(
                (
                    p
                    for p in peers
                    if p.mesh_ip == lender_ip
                    and p.outbound is not None
                    and p.outbound.link_id == link.link_id
                    and p.outbound.port == link.port
                ),
                None,
            )
            if mine is not None and mine.reachable:
                return mine, "ready"
            if mine is None and cached:
                return None, "stale"
        elif cached:
            return None, "unreadable"
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None, "timeout"
        await asyncio.sleep(min(LINK_POLL_INTERVAL_S, remaining))
        if deadline - loop.time() <= 0:
            return None, "timeout"  # do not spend another read after the budget is gone


# ---- the cache --------------------------------------------------------------


def _cached(key: tuple[str, str]) -> Link | None:
    link = _links.get(key)
    if link is None:
        return None
    if link.ended or link.deadline - _now() < MIN_REUSE_REMAINING_S:
        _forget(link)
        return None
    return link


def _remember(link: Link) -> None:
    key = (link.circle_id, link.model)
    for other_key, other in list(_links.items()):
        # Two links to one lender cannot both stand: the coordinator replaces the
        # old one when the new one is made.
        if other_key == key or other.lender_ip == link.lender_ip:
            _forget(other)
    _links[key] = link


def _forget(link: Link) -> None:
    """Stop using and renewing a link. It lapses by itself, or the coordinator has
    already ended it."""
    link.ended = True
    key = (link.circle_id, link.model)
    if _links.get(key) is link:
        del _links[key]
    renewer = link.renewer
    link.renewer = None
    if renewer is not None and renewer is not asyncio.current_task():
        renewer.cancel()


def _end_in_background(link: Link) -> None:
    """Forget the link and tell the coordinator to end it, without making the
    request wait for that."""
    _forget(link)
    task = asyncio.ensure_future(_delete_link(link))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _delete_link(link: Link) -> None:
    try:
        async with asyncio.timeout(DELETE_TIMEOUT_S):
            async with _new_client(timeout=DELETE_TIMEOUT_S) as client:
                await client.delete(
                    f"{link.base}/me/circles/{link.circle_id}/links/{link.link_id}",
                    headers={**pool_routing._headers(link.base), "Accept": "application/json"},
                )
    except Exception:  # noqa: BLE001 - the link also lapses on its own
        logger.debug("circle_link_end_failed", exc_info=True)


# ---- one request over a link ------------------------------------------------


class _CappedStream(httpx.AsyncByteStream):
    """The lender's response body, cut off past a byte limit. httpx buffers a line
    until it ends, so without a limit a lender that never ends one fills memory."""

    def __init__(self, inner: httpx.AsyncByteStream, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for part in self._inner:
            total += len(part)
            if total > self._limit:
                raise RemoteStreamError("The circle member's computer sent too much.", "stream_protocol_error")
            yield part

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _lender_error_code(response: httpx.Response) -> str | None:
    try:
        body = await response.aread()
    except httpx.HTTPError:
        return None
    try:
        parsed = json.loads(body[:4096])
    except ValueError:
        return None
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    return _clean_code(detail.get("code") if isinstance(detail, dict) else None)


async def _over_link(plan: Plan, link: Link, transport: str | None) -> AsyncIterator[dict]:
    """POST the request to the lender and yield its chunks. Raises DirectFailed
    for a failure before anything was yielded; after that, RemoteStreamError."""
    url = f"http://{link.lender_ip}:{link.port}{CHAT_PATH}"
    headers = {
        "Authorization": f"Bearer {link.token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REQUEST_TIMEOUT_S
    committed = False
    held: list[dict] = []
    template = _stream_template(plan.model)
    link.active += 1
    try:
        link.last_used = _now()
        _ensure_renewer(link)
        async with asyncio.timeout_at(min(deadline, loop.time() + plan.first_token_budget)) as budget:
            # trust_env off: the prompt and the token must never go through an
            # HTTP proxy named in the environment.
            async with _new_client(
                trust_env=False,
                timeout=httpx.Timeout(None, connect=CONNECT_TIMEOUT_S, write=CONNECT_TIMEOUT_S, pool=CONNECT_TIMEOUT_S),
            ) as client:
                async with client.stream("POST", url, content=plan.body, headers=headers) as response:
                    if response.status_code != 200:
                        code = await _lender_error_code(response)
                        raise DirectFailed(f"lender_{response.status_code}_{code or 'none'}")
                    if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                        # httpx would unpack it, and a small body can unpack to gigabytes.
                        raise DirectFailed("lender_encoded_response")
                    response.stream = _CappedStream(response.stream, _MAX_RESPONSE_BYTES)  # type: ignore[arg-type]
                    async with aclosing(openai_chunks(response, plan.model, coordinator=True)) as chunks:
                        async for raw_chunk in chunks:
                            chunk, useful, finished = _rebuilt(template, raw_chunk)
                            delta = chunk["choices"][0]["delta"]
                            if finished:
                                chunk = _with_receipt(chunk, path="direct", transport=transport)
                            if not committed:
                                if not useful and not finished:
                                    # A role-only opening is not output; keep it back
                                    # (one is enough) until there is something a replay
                                    # would lose. A chunk with nothing left is dropped.
                                    if delta and not held:
                                        held.append(chunk)
                                    continue
                                committed = True
                                for early in held:
                                    yield early
                                held.clear()
                            elif not delta and not finished:
                                continue  # nothing left of it: not output, and no time bought
                            if useful or finished:
                                budget.reschedule(None if finished else min(deadline, loop.time() + IDLE_TIMEOUT_S))
                            yield chunk
    except (httpx.HTTPError, RemoteStreamError, TimeoutError, OSError) as exc:
        if isinstance(exc, RemoteStreamError):
            # Its text and category may be the lender's: say it in our own words.
            category = fixed_category(exc.category)
            if committed:
                raise RemoteStreamError(ERROR_TEXT[category], category) from None
            raise DirectFailed(f"lender_stream_{category}") from None
        if committed:
            category = "stream_timeout" if isinstance(exc, TimeoutError) else "peer_unreachable"
            raise RemoteStreamError(ERROR_TEXT[category], category) from None
        raise DirectFailed(type(exc).__name__) from None
    finally:
        link.active -= 1
        link.last_used = _now()


# ---- keeping a link alive ---------------------------------------------------


def _ensure_renewer(link: Link) -> None:
    if link.ended or (link.renewer is not None and not link.renewer.done()):
        return
    link.renewer = asyncio.ensure_future(_renew_loop(link))


def _wanted(link: Link) -> bool:
    return link.active > 0 or _now() - link.last_used <= RENEW_ACTIVE_WINDOW_S


async def _renew_loop(link: Link) -> None:
    """Renew at renew_after_s while the link is in use, or was within two minutes.
    Never raises: a link that cannot be renewed just lapses."""
    try:
        while not link.ended:
            wait = link.next_renew_at - _now()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            if _now() >= link.deadline:
                _forget(link)
                return
            if not _wanted(link):
                return  # idle; a later request starts this again if the link is still good
            if await _renew_once(link) == "final":
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("circle_link_renew_failed error=%s", type(exc).__name__)


async def _renew_once(link: Link) -> str:
    """One renew call. "final" when renewing must stop, else "again"."""
    sent_at = _now()
    try:
        async with asyncio.timeout(RENEW_TIMEOUT_S):
            async with _new_client(timeout=RENEW_TIMEOUT_S) as client:
                response = await client.post(
                    f"{link.base}/me/circles/{link.circle_id}/links/{link.link_id}/renew",
                    json={},
                    headers={**pool_routing._headers(link.base), "Accept": "application/json"},
                )
    except (httpx.HTTPError, OSError, TimeoutError):
        link.next_renew_at = _now() + RENEW_RETRY_S
        return "again"
    status = response.status_code
    if status == 200:
        try:
            renewed = LinkRenewed.model_validate_json(response.content)
        except ValidationError:
            link.next_renew_at = _now() + RENEW_RETRY_S
            return "again"
        link.deadline = sent_at + renewed.ttl_s
        link.renew_after_s = renewed.renew_after_s
        link.next_renew_at = sent_at + renewed.renew_after_s
        return "again"
    if status in (410, 404):
        # 410 link_ended is final: the link is over, whatever the reason. Not
        # found says the same.
        _forget(link)
        return "final"
    if status == 429:
        link.next_renew_at = _now() + _retry_after(response)
        return "again"
    if status == 401:
        return "final"  # signed out: the link lapses; a later request tries again
    link.next_renew_at = _now() + RENEW_RETRY_S
    return "again"
