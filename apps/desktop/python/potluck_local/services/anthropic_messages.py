"""The Anthropic Messages API, spoken over the engine's OpenAI-shaped chat.

``parse_request`` turns a POST /v1/messages body into the messages, tools and
options the gateway's routing code already takes (routers/gateway.py ChatJob).
``message_from_completion`` and ``MessageStream`` turn what comes back, a whole
chat.completion or a stream of chat.completion.chunk dicts, into an Anthropic
message or Anthropic server-sent events. Routing, auth and HTTP live elsewhere
(routers/anthropic.py); nothing here does I/O.

Request
-------
``system`` (a string or text blocks) and any message with role "system" become one
leading system message, joined with a blank line, because most local chat
templates only take a system message first. A user message that holds tool
results and text becomes the tool messages first, then one user message with the
text. An image inside a tool result becomes a line of text saying so; an image or
document anywhere else is refused, as is any block type the engine cannot read.
Assistant ``thinking`` and ``redacted_thinking`` blocks are dropped. ``cache_control``
is ignored everywhere. Neighbouring plain-text messages of the same role are joined
with a blank line, because some chat templates refuse anything but strict user and
assistant turns; a message with tool calls and a tool message are left alone.

Only tools that carry an ``input_schema`` (the client's own) can run here.
Anthropic's server tools and built-in client tools have none and are refused, by
type. ``tool_choice`` maps to the OpenAI forms: auto, any (= required), tool
(a named function) and none. ``disable_parallel_tool_use`` keeps only the first call
of a reply.

Accepted and ignored: ``metadata``, ``context_management``, ``safeguards``,
``container``, ``mcp_servers``, ``service_tier``, ``output_config`` without a
``format``, and any unknown field. ``output_config.format`` and ``output_format``
(structured output) are refused, because ignoring them would hand the client a
reply in the wrong shape. ``top_p`` and ``top_k`` are accepted and not applied:
the engine's chat path takes temperature only. ``stop_sequences`` reach this
machine's own model and another of the user's machines; a reply from a circle or
the pool is not cut at them.

``thinking`` of type "enabled" or "adaptive" (unless its ``display`` is "omitted")
returns the model's reasoning as a ``thinking`` block. Otherwise the reasoning
text is left out of the reply. The model is not told to stop reasoning.

Reply
-----
Content blocks come in this order: thinking (if shown), text (if any), then one
``tool_use`` block per call, with the call id the engine made. ``stop_reason`` is
"tool_use" when there are calls, "max_tokens" when the engine stopped on length,
and "end_turn" otherwise. ``stop_sequence`` is always null: the engine cannot
tell a stop string from a natural end.

Usage counts are the engine's when it gave them. When it gave none (a reply from
another machine, or any streamed reply from this one) they are estimated at about
four characters a token, rounded up, and the router adds ``X-Potluck-Usage: estimated``.

Nothing here logs. The messages built here name block types and counts, never the
client's own text.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from potluck_local.services.gateway_request import Prepared, RequestError, normalise_message, prepare
from potluck_local.services.token_estimate import engine_count as _count
from potluck_local.services.token_estimate import prompt_text as _prompt_text
from potluck_local.services.token_estimate import tokens_for_chars
from potluck_local.services.tool_calls import new_call_id

MAX_BODY_BYTES = 32 * 1024 * 1024
# Seconds of silence after which a stream sends a ping.
PING_INTERVAL_S = 10.0
IMAGE_LEFT_OUT = "[image left out: this model reads text only]"

_JOIN = "\n\n"
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _shown(value: object) -> str:
    """A client-supplied type or name, safe to repeat in a message."""
    return value if isinstance(value, str) and _NAME.fullmatch(value) else "unknown"


def shown_name(value: object) -> str:
    """A client-supplied name that is safe to log: itself when it is a short plain
    name, otherwise "unknown"."""
    return _shown(value)


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MessagesRequest:
    model: str | None
    prepared: Prepared
    max_tokens: int
    temperature: float
    stop: list[str]
    stream: bool
    show_thinking: bool
    first_call_only: bool


def _system_text(content: Any, where: str) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
                raise RequestError("invalid_system", f"{where} must be a string or a list of text blocks.")
            texts.append(block["text"])
        return _JOIN.join(t for t in texts if t)
    raise RequestError("invalid_system", f"{where} must be a string or a list of text blocks.")


def _result_text(content: Any) -> str:
    """The text of a tool_result's content: a string, or a list of text and image blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise RequestError("invalid_tool_result", "The content of a tool_result must be a string or a list of blocks.")
    texts = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "image":
            texts.append(IMAGE_LEFT_OUT)
        else:
            raise RequestError(
                "unsupported_content_block",
                f"A tool_result holds a block of type '{_shown(kind)}'. Only text and image blocks are supported.",
            )
    return _JOIN.join(t for t in texts if t)


def _tool_message(block: dict) -> dict:
    call_id = block.get("tool_use_id")
    if not isinstance(call_id, str) or not call_id:
        raise RequestError("invalid_tool_result", "A tool_result block needs a tool_use_id.")
    text = _result_text(block.get("content"))
    if not text and block.get("is_error") is True:
        text = "Error"
    return normalise_message("tool", text, tool_call_id=call_id)


def _user_messages(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [normalise_message("user", content)]
    if not isinstance(content, list):
        raise RequestError("invalid_content", "The content of a user message must be a string or a list of blocks.")
    results: list[dict] = []
    texts: list[str] = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "tool_result":
            results.append(_tool_message(block))
        elif kind in ("image", "document"):
            raise RequestError(
                "unsupported_content_block",
                f"A user message holds a block of type '{kind}'. This model reads text only.",
            )
        else:
            raise RequestError("unsupported_content_block", f"A user message holds a block of type '{_shown(kind)}', which is not supported.")
    text = _JOIN.join(t for t in texts if t)
    return results + ([normalise_message("user", text)] if text else [])


def _assistant_message(content: Any) -> dict | None:
    """An assistant turn in the OpenAI shape, or None when nothing is left of it
    once thinking blocks are dropped."""
    texts: list[str] = []
    calls: list[dict] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif kind == "tool_use":
                call_id, name, args = block.get("id"), block.get("name"), block.get("input")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                    raise RequestError("invalid_tool_use", "A tool_use block needs an id and a name.")
                if args is None:
                    args = {}
                if not isinstance(args, dict):
                    raise RequestError("invalid_tool_use", "The input of a tool_use block must be a JSON object.")
                calls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": args}})
            elif kind in ("thinking", "redacted_thinking"):
                continue
            else:
                raise RequestError("unsupported_content_block", f"An assistant message holds a block of type '{_shown(kind)}', which is not supported.")
    else:
        raise RequestError("invalid_content", "The content of an assistant message must be a string or a list of blocks.")
    text = _JOIN.join(t for t in texts if t)
    if not text.strip() and not calls:
        return None
    return normalise_message("assistant", text, tool_calls_=calls or None)


def _plain(message: dict) -> bool:
    """A user or assistant message that holds text only."""
    return message["role"] in ("user", "assistant") and "tool_calls" not in message


def _join_neighbours(messages: list[dict]) -> list[dict]:
    """Neighbouring plain-text messages of the same role as one message, joined with a
    blank line. Some chat templates refuse anything but strict user and assistant
    turns, and a client can send two in a row, or leave two in a row by dropping an
    assistant message that held only thinking. A message with tool calls and a tool
    message are never joined."""
    out: list[dict] = []
    for message in messages:
        last = out[-1] if out else None
        if last is not None and _plain(last) and _plain(message) and last["role"] == message["role"]:
            out[-1] = {**last, "content": _JOIN.join(t for t in (last["content"], message["content"]) if t)}
        else:
            out.append(message)
    return out


join_neighbours = _join_neighbours  # shared with services/responses_api.py


def _messages(raw: Any) -> tuple[list[dict], list[str]]:
    """``(messages, texts of the role "system" entries)``."""
    if not isinstance(raw, list) or not raw:
        raise RequestError("invalid_messages", "messages must be a non-empty list.")
    out: list[dict] = []
    system: list[str] = []
    for entry in raw:
        role = entry.get("role") if isinstance(entry, dict) else None
        content = entry.get("content") if isinstance(entry, dict) else None
        if role == "system":
            system.append(_system_text(content, "A message with role 'system'"))
        elif role == "user":
            out.extend(_user_messages(content))
        elif role == "assistant":
            message = _assistant_message(content)
            if message is not None:
                out.append(message)
        else:
            raise RequestError("invalid_role", "Each message needs the role 'user' or 'assistant'.")
    return _join_neighbours(out), system


def _tools(raw: Any) -> list[dict] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise RequestError("invalid_tools", "tools must be a list.")
    tools = []
    for item in raw:
        if not isinstance(item, dict):
            raise RequestError("invalid_tools", "Each tool must be an object.")
        if "input_schema" not in item:
            raise RequestError(
                "unsupported_tool",
                f"The tool '{_shown(item.get('name'))}' has type '{_shown(item.get('type') or 'custom')}' and no input_schema. "
                "Only tools the client defines, with an input_schema, can run here. "
                "Anthropic's server tools and built-in tools cannot.",
            )
        function: dict = {"name": item.get("name"), "parameters": item["input_schema"]}
        if isinstance(item.get("description"), str):
            function["description"] = item["description"]
        tools.append({"type": "function", "function": function})
    return tools


def _tool_choice(raw: Any) -> tuple[Any, bool]:
    """``(OpenAI tool_choice, whether only the first call of a reply is kept)``."""
    if raw is None:
        return None, False
    if not isinstance(raw, dict):
        raise RequestError("invalid_tool_choice", "tool_choice must be an object with a type.")
    kind = raw.get("type")
    first_only = raw.get("disable_parallel_tool_use") is True
    if kind == "auto":
        return "auto", first_only
    if kind == "any":
        return "required", first_only
    if kind == "none":
        return "none", False
    if kind == "tool":
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise RequestError("invalid_tool_choice", "tool_choice of type 'tool' needs a name.")
        return {"type": "function", "function": {"name": name}}, first_only
    raise RequestError("invalid_tool_choice", "tool_choice type must be 'auto', 'any', 'tool' or 'none'.")


def _refuse_structured_output(body: dict) -> None:
    config = body.get("output_config")
    if body.get("output_format") is not None or (isinstance(config, dict) and config.get("format") is not None):
        raise RequestError(
            "unsupported_output_format",
            "Structured output (output_config.format) is not supported. Ask for the format in the prompt instead.",
        )


def _stop_sequences(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        raise RequestError("invalid_stop_sequences", "stop_sequences must be a list of strings.")
    return [s for s in raw if s]


def parse_request(body: Any, *, default_max_tokens: int, default_temperature: float) -> MessagesRequest:
    """Check a /v1/messages body and turn it into what the gateway routes.
    Raises ``RequestError`` with a message fit for a 400."""
    if not isinstance(body, dict):
        raise RequestError("invalid_request", "The request body must be a JSON object.")
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        raise RequestError("invalid_model", "model must be a string.")
    _refuse_structured_output(body)

    messages, role_system = _messages(body.get("messages"))
    system = _JOIN.join(t for t in [_system_text(body.get("system"), "system"), *role_system] if t)
    if system:
        messages.insert(0, normalise_message("system", system))
    tool_choice, first_only = _tool_choice(body.get("tool_choice"))
    prepared = prepare(messages, _tools(body.get("tools")), tool_choice)

    max_tokens = body.get("max_tokens", default_max_tokens)
    if type(max_tokens) is not int or max_tokens < 1:
        raise RequestError("invalid_max_tokens", "max_tokens must be a whole number of at least 1.")
    temperature = body.get("temperature", default_temperature)
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
        raise RequestError("invalid_temperature", "temperature must be a number from 0 to 2.")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise RequestError("invalid_stream", "stream must be true or false.")
    thinking = body.get("thinking")
    shows = isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive") and thinking.get("display") != "omitted"
    return MessagesRequest(
        model=model or None, prepared=prepared, max_tokens=max_tokens, temperature=float(temperature),
        stop=_stop_sequences(body.get("stop_sequences")), stream=stream, show_thinking=shows,
        first_call_only=first_only,
    )


def prompt_text(request: MessagesRequest) -> str:
    """The text a prompt token count is taken over: the system and message text, the
    calls in the history, and the tools the model is shown, as JSON. The chat
    template's own markup is not in it."""
    return _prompt_text(request.prepared.messages, request.prepared.tools)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def error_kind(status: int) -> str:
    if status in (400, 409, 422):
        return "invalid_request_error"
    return {
        401: "authentication_error", 403: "permission_error", 404: "not_found_error",
        413: "request_too_large", 429: "rate_limit_error", 503: "overloaded_error", 529: "overloaded_error",
    }.get(status, "api_error")


def error_body(status: int, message: str) -> dict:
    return {"type": "error", "error": {"type": error_kind(status), "message": message}}


def error_detail(detail: Any) -> tuple[str, str | None]:
    """``(message, code)`` from an HTTPException detail: text, or ``{code, message}``."""
    if isinstance(detail, str):
        return detail, None
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        code = detail.get("code")
        return detail["message"], code if isinstance(code, str) else None
    return "The request failed.", None


def prompt_too_long(tokens: int, window: int) -> str:
    """The message Claude Code looks for. The engine refuses a prompt of ``window``
    tokens or more, so the largest it takes is one less."""
    return f"prompt is too long: {tokens} tokens > {window - 1} maximum"


@dataclass(frozen=True)
class Failure:
    status: int
    message: str
    code: str | None = None


# ---------------------------------------------------------------------------
# Whole replies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplyOptions:
    model: str  # echoed back: the model string the client sent
    show_thinking: bool = False
    first_call_only: bool = False
    input_estimate: int = 0  # prompt tokens to report when the engine gave no count


def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def _usage(input_tokens: int, output_tokens: int) -> dict:
    return {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }


def _stop_reason(calls: int, finish: object) -> str:
    if calls:
        return "tool_use"
    return "max_tokens" if finish == "length" else "end_turn"


def _tool_input(arguments: object) -> dict:
    try:
        value = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def message_from_completion(result: dict, options: ReplyOptions) -> tuple[dict, bool]:
    """``(Anthropic message, whether any usage figure is an estimate)`` for a shaped
    chat.completion (see gateway_reply.shape_completion)."""
    choices = result.get("choices") if isinstance(result, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    reasoning, text = message.get("reasoning_content"), message.get("content")
    reasoning = reasoning if isinstance(reasoning, str) else ""
    text = text if isinstance(text, str) else ""

    content: list[dict] = []
    if options.show_thinking and reasoning:
        content.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    if text:
        content.append({"type": "text", "text": text})
    chars = len(reasoning) + len(text)
    calls = []
    for call in message.get("tool_calls") or ():
        function = call.get("function") if isinstance(call, dict) and isinstance(call.get("function"), dict) else {}
        if not isinstance(function.get("name"), str):
            continue
        arguments = function.get("arguments")
        chars += len(function["name"]) + (len(arguments) if isinstance(arguments, str) else 0)
        call_id = call.get("id") if isinstance(call.get("id"), str) else new_call_id()
        calls.append({"type": "tool_use", "id": call_id, "name": function["name"], "input": _tool_input(arguments)})
    if options.first_call_only:
        calls = calls[:1]
    content.extend(calls)

    usage = result.get("usage") if isinstance(result, dict) and isinstance(result.get("usage"), dict) else {}
    given_in, given_out = _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))
    reply = {
        "id": new_message_id(), "type": "message", "role": "assistant", "model": options.model,
        "content": content, "stop_reason": _stop_reason(len(calls), choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": _usage(
            options.input_estimate if given_in is None else given_in,
            tokens_for_chars(chars) if given_out is None else given_out,
        ),
    }
    return reply, given_in is None or given_out is None


# ---------------------------------------------------------------------------
# Streamed replies
# ---------------------------------------------------------------------------


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


PING = sse("ping", {"type": "ping"})


def error_event(status: int, message: str) -> str:
    return sse("error", error_body(status, message))


class _Translator:
    """Turns shaped chat.completion.chunk dicts into Anthropic events, one block at a
    time: a block is closed before the next opens, so blocks never interleave."""

    def __init__(self, options: ReplyOptions) -> None:
        self._options = options
        self._index = -1  # index of the last block opened
        self._open: str | None = None  # "thinking", "text" or "tool_use" while a block is open
        self._call_index: object = None  # the engine's index of the call being streamed
        self._dropped: set = set()  # indexes of calls left out (disable_parallel_tool_use)
        self._calls = 0
        self._finish: str | None = None
        self._out_tokens: int | None = None
        self._chars = 0  # characters generated, for the estimate; reasoning counts even when hidden

    def feed(self, chunk: object) -> list[str]:
        if not isinstance(chunk, dict):
            return []
        usage = chunk.get("usage")
        if isinstance(usage, dict) and _count(usage.get("completion_tokens")) is not None:
            self._out_tokens = usage["completion_tokens"]
        choices = chunk.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        if choice is None:
            return []
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        out: list[str] = []
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            self._chars += len(reasoning)
            if self._options.show_thinking:
                out += self._piece("thinking", "thinking_delta", reasoning)
        text = delta.get("content")
        if isinstance(text, str) and text:
            self._chars += len(text)
            out += self._piece("text", "text_delta", text)
        for call in delta.get("tool_calls") or ():
            out += self._tool_call(call)
        reason = choice.get("finish_reason")
        if isinstance(reason, str) and reason:
            self._finish = reason
        return out

    def _piece(self, kind: str, delta_type: str, text: str) -> list[str]:
        out = []
        if self._open != kind:
            out += self._close()
            self._index += 1
            self._open = kind
            block = {"type": kind, kind: ""}
            if kind == "thinking":
                block["signature"] = ""
            out.append(sse("content_block_start", {
                "type": "content_block_start", "index": self._index, "content_block": block,
            }))
        out.append(sse("content_block_delta", {
            "type": "content_block_delta", "index": self._index, "delta": {"type": delta_type, kind: text},
        }))
        return out

    def _tool_call(self, call: object) -> list[str]:
        if not isinstance(call, dict):
            return []
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        index = call.get("index") if type(call.get("index")) is int else 0
        name, arguments = function.get("name"), function.get("arguments")
        if index in self._dropped:
            return []
        out = []
        if isinstance(name, str) and name and (self._open != "tool_use" or index != self._call_index):
            if self._options.first_call_only and self._calls:
                self._dropped.add(index)
                return []
            out += self._close()
            self._index += 1
            self._open, self._call_index = "tool_use", index
            self._calls += 1
            self._chars += len(name)
            call_id = call.get("id") if isinstance(call.get("id"), str) else new_call_id()
            out.append(sse("content_block_start", {
                "type": "content_block_start", "index": self._index,
                "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
            }))
        if isinstance(arguments, str) and arguments and self._open == "tool_use" and index == self._call_index:
            self._chars += len(arguments)
            out.append(sse("content_block_delta", {
                "type": "content_block_delta", "index": self._index,
                "delta": {"type": "input_json_delta", "partial_json": arguments},
            }))
        return out

    def _close(self) -> list[str]:
        if self._open is None:
            return []
        out = []
        if self._open == "thinking":
            out.append(sse("content_block_delta", {
                "type": "content_block_delta", "index": self._index,
                "delta": {"type": "signature_delta", "signature": ""},
            }))
        out.append(sse("content_block_stop", {"type": "content_block_stop", "index": self._index}))
        self._open = None
        return out

    def finish(self) -> list[str]:
        out = self._close()
        tokens = self._out_tokens if self._out_tokens is not None else tokens_for_chars(self._chars)
        out.append(sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": _stop_reason(self._calls, self._finish), "stop_sequence": None},
            "usage": {"output_tokens": tokens},
        }))
        out.append(sse("message_stop", {"type": "message_stop"}))
        return out


class MessageStream:
    """One streamed reply: the engine's chunks in, Anthropic events out.

    ``start`` waits a short while for the engine's first word, so a failure there
    can still be a plain HTTP error, never a 200 stream. If the engine is quiet
    longer than that (a long prompt takes time to process) the stream begins anyway,
    and a later failure is an ``error`` event. ``events`` then yields the events,
    with a ping whenever ``ping_interval`` seconds pass with nothing sent. A ping
    never waits for the engine and the engine's chunks never wait for a ping.

    ``describe`` turns a failure, an exception or the local engine's ``__error__``
    chunk, into a ``Failure``. Call ``aclose`` if ``events`` is never run to its end.
    """

    def __init__(
        self,
        upstream: AsyncIterator,
        options: ReplyOptions,
        input_tokens: int,
        describe: Callable[[object], Failure],
        *,
        ping_interval: float = PING_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._upstream = upstream
        self._options = options
        self._input_tokens = input_tokens
        self._describe = describe
        self._interval = ping_interval
        self._clock = clock
        self._pending: asyncio.Task | None = None  # a read of the engine that has not finished
        self._buffered: tuple[str, object] | None = None  # an outcome read by ``start``

    async def _read(self) -> tuple[str, object]:
        try:
            chunk = await self._upstream.__anext__()
        except StopAsyncIteration:
            return "end", None
        except Exception as exc:  # noqa: BLE001 - described to the client by the router
            return "failure", exc
        if isinstance(chunk, dict) and "__error__" in chunk:
            return "failure", chunk
        return "chunk", chunk

    async def start(self, window: float) -> Failure | None:
        """The failure that came within ``window`` seconds, or None: the stream may begin."""
        self._pending = asyncio.get_running_loop().create_task(self._read())
        await asyncio.wait({self._pending}, timeout=window)
        if not self._pending.done():
            return None
        outcome, self._pending = self._pending.result(), None
        if outcome[0] == "failure":
            return self._describe(outcome[1])
        self._buffered = outcome
        return None

    async def events(self) -> AsyncIterator[str]:
        translator = _Translator(self._options)
        try:
            yield sse("message_start", {"type": "message_start", "message": {
                "id": new_message_id(), "type": "message", "role": "assistant", "model": self._options.model,
                "content": [], "stop_reason": None, "stop_sequence": None, "usage": _usage(self._input_tokens, 0),
            }})
            yield PING
            last_sent = self._clock()
            while True:
                outcome, self._buffered = self._buffered, None
                if outcome is None:
                    if self._pending is None:
                        self._pending = asyncio.get_running_loop().create_task(self._read())
                    await asyncio.wait({self._pending}, timeout=max(self._interval - (self._clock() - last_sent), 0))
                    if not self._pending.done():
                        yield PING
                        last_sent = self._clock()
                        continue
                    outcome, self._pending = self._pending.result(), None
                kind, value = outcome
                if kind == "end":
                    for event in translator.finish():
                        yield event
                    return
                if kind == "failure":
                    failure = self._describe(value)
                    yield error_event(failure.status, failure.message)
                    return
                sent = translator.feed(value)
                for event in sent:
                    yield event
                if sent:
                    last_sent = self._clock()
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        close = getattr(self._upstream, "aclose", None)
        if close is not None:
            await close()
