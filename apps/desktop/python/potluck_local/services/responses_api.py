"""The OpenAI Responses API, spoken over the engine's OpenAI-shaped chat.

``parse_request`` turns a POST /v1/responses body into the messages, tools and
options the gateway's routing code already takes (routers/gateway.py ChatJob).
``response_from_completion`` and ``ResponseStream`` turn what comes back, a whole
chat.completion or a stream of chat.completion.chunk dicts, into a Response object
or Responses server-sent events. Routing, auth and HTTP live elsewhere
(routers/responses.py); nothing here does I/O.

Request
-------
``instructions`` and any input message with role "system" or "developer" become one
leading system message, joined with a blank line (most local chat templates only
take a system message first). ``input`` is a string, or a list of items:

  message                   user and assistant turns; input_text, output_text, text
                            and refusal parts. Images and files are refused.
  function_call             an assistant tool call. Calls next to each other, and a
                            call right after an assistant message, join one turn.
  function_call_output      a tool message.
  custom_tool_call(_output) the same, for a freeform tool (see below).
  reasoning, compaction     dropped.

``item_reference`` and any other item type are refused. Neighbouring plain-text
messages of the same role are joined (see anthropic_messages.join_neighbours).

``previous_response_id`` and ``conversation`` are refused with a 400: Potluck keeps
no response store, so the client sends the whole conversation in ``input`` every
turn (Codex does). ``background`` and ``text.format`` other than plain text are
refused too, because ignoring them would hand the client a reply in the wrong shape.

Tools
-----
Function tools ({"type": "function", "name", "description", "parameters"}) run as
they are. A ``namespace`` tool (Codex groups its MCP and sub-agent tools this way) is
flattened to one function per inner tool, named ``<namespace>__<name>``, and a call to
it comes back as a function_call with ``namespace`` and the inner ``name``. A
``custom`` (freeform) tool, such as Codex's apply_patch on models that use it, runs as
a function with one string parameter, ``input``; the model's call comes back as a
custom_tool_call whose ``input`` is that string. Any other tool type (web_search,
file_search, ...) is hosted by OpenAI and cannot run here. It is left out and named
in ``ignored_tools``; the router reports them in X-Potluck-Ignored-Tools.

``tool_choice`` takes "auto", "none", "required", {"type": "function", "name"},
{"type": "custom", "name"} and {"type": "allowed_tools", "mode"} (the mode only).
``parallel_tool_calls: false`` keeps only the first call of a reply.

Accepted and ignored: ``store``, ``metadata`` (echoed), ``include``, ``reasoning``,
``prompt_cache_key``, ``client_metadata``, ``service_tier``, ``truncation``, ``user``,
``top_p`` and any unknown field. ``top_p`` is not applied: the engine's chat path
takes temperature only.

Reply
-----
Output items come in this order: one assistant ``message`` with an ``output_text``
part (if there is text), then one ``function_call`` or ``custom_tool_call`` per call,
with the call id the engine made. The model's reasoning is never returned. The status
is "incomplete" with reason "max_output_tokens" when the engine stopped on length
and made no call, and "completed" otherwise.

Usage counts are the engine's when it gave them, otherwise estimated at about four
characters a token, and the router adds ``X-Potluck-Usage: estimated``.

Nothing here logs. The messages built here name item types and counts, never the
client's own text.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from potluck_local.services import anthropic_messages as am
from potluck_local.services.anthropic_messages import Failure, shown_name
from potluck_local.services.gateway_request import Prepared, RequestError, normalise_message, prepare
from potluck_local.services.token_estimate import engine_count as _count
from potluck_local.services.token_estimate import prompt_text as _prompt_text
from potluck_local.services.token_estimate import tokens_for_chars
from potluck_local.services.tool_calls import new_call_id, valid_tool_name

MAX_BODY_BYTES = am.MAX_BODY_BYTES
# Seconds of silence after which a stream sends a comment line, so that a client or
# proxy with an idle timeout does not give up on a long prompt.
KEEPALIVE_INTERVAL_S = am.PING_INTERVAL_S
KEEPALIVE = ": keep-alive\n\n"
IMAGE_LEFT_OUT = am.IMAGE_LEFT_OUT

_JOIN = "\n\n"
# Item types a client sends back that carry nothing the model reads.
_DROPPED_ITEMS = frozenset({"reasoning", "compaction", "compaction_summary", "web_search_call", "ghost_snapshot"})
_TEXT_PARTS = frozenset({"input_text", "output_text", "text"})
_CUSTOM_PARAMETERS = {
    "type": "object",
    "properties": {"input": {"type": "string", "description": "The raw text to pass to this tool."}},
    "required": ["input"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolNames:
    """How the function names the model sees map back to the client's tools."""

    custom: frozenset[str] = frozenset()  # freeform tools, by name
    spaces: dict[str, tuple[str, str]] = field(default_factory=dict)  # flat name -> (namespace, name)

    def flat(self, namespace: object, name: str) -> str:
        """The function name for a call the client made earlier."""
        if not isinstance(namespace, str) or not namespace:
            return name
        for flat, pair in self.spaces.items():
            if pair == (namespace, name):
                return flat
        return _join_namespace(namespace, name)

    def split(self, flat: str) -> tuple[str | None, str]:
        pair = self.spaces.get(flat)
        return (pair[0], pair[1]) if pair else (None, flat)


@dataclass(frozen=True)
class ResponsesRequest:
    model: str | None
    prepared: Prepared
    max_tokens: int | None  # None: the gateway's default
    temperature: float
    stream: bool
    first_call_only: bool
    names: ToolNames
    ignored_tools: tuple[str, ...]
    echo: dict  # request settings a Response reports back


def _join_namespace(namespace: str, name: str) -> str:
    return namespace + name if namespace.endswith("_") else f"{namespace}__{name}"


def _unique(base: str, taken: set[str]) -> str:
    base = base[:64]
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f"_{n}"
        candidate = base[: 64 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        n += 1


def _function_tool(item: dict, where: str) -> dict:
    name = item.get("name")
    if not valid_tool_name(name):
        raise RequestError("invalid_tools", f"{where} needs a name of 1 to 64 letters, digits, underscores, dots or dashes.")
    function: dict = {"name": name}
    if isinstance(item.get("description"), str):
        function["description"] = item["description"]
    if item.get("parameters") is not None:
        function["parameters"] = item["parameters"]
    return {"type": "function", "function": function}


def _tools(raw: Any) -> tuple[list[dict] | None, ToolNames, tuple[str, ...]]:
    """``(tools in the chat shape, name map, types left out)``."""
    if raw is None:
        return None, ToolNames(), ()
    if not isinstance(raw, list):
        raise RequestError("invalid_tools", "tools must be a list.")
    tools: list[dict] = []
    custom: set[str] = set()
    spaces: dict[str, tuple[str, str]] = {}
    ignored: list[str] = []
    top_level = {t.get("name") for t in raw if isinstance(t, dict) and t.get("type") == "function"}
    taken: set[str] = {n for n in top_level if isinstance(n, str)}
    for item in raw:
        if not isinstance(item, dict):
            raise RequestError("invalid_tools", "Each tool must be an object.")
        kind = item.get("type")
        if kind == "function":
            tools.append(_function_tool(item, "A function tool"))
        elif kind == "custom":
            tool = _function_tool(item, "A custom tool")
            tool["function"]["parameters"] = _CUSTOM_PARAMETERS
            tools.append(tool)
            custom.add(tool["function"]["name"])
            taken.add(tool["function"]["name"])
        elif kind == "namespace":
            space = item.get("name")
            inner = item.get("tools")
            if not valid_tool_name(space) or not isinstance(inner, list):
                raise RequestError("invalid_tools", "A namespace tool needs a valid name and a list of tools.")
            for member in inner:
                if not isinstance(member, dict) or member.get("type") != "function":
                    raise RequestError("invalid_tools", f"The namespace '{shown_name(space)}' holds a tool that is not a function. Only function tools can sit in a namespace.")
                tool = _function_tool(member, "A function tool in a namespace")
                local = tool["function"]["name"]
                flat = _unique(_join_namespace(space, local), taken)
                taken.add(flat)
                spaces[flat] = (space, local)
                tool["function"]["name"] = flat
                tools.append(tool)
        else:
            ignored.append(shown_name(kind))
    return tools, ToolNames(frozenset(custom), spaces), tuple(ignored)


def _tool_choice(raw: Any, names: ToolNames) -> Any:
    """The chat-completions form of ``tool_choice``, for ``prepare`` to check."""
    if raw is None or isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        raise RequestError("invalid_tool_choice", "tool_choice must be a string or an object.")
    kind = raw.get("type")
    if kind == "allowed_tools":
        mode = raw.get("mode", "auto")
        if mode not in ("auto", "required"):
            raise RequestError("invalid_tool_choice", "tool_choice of type 'allowed_tools' needs a mode of 'auto' or 'required'.")
        return mode
    if kind in ("function", "custom"):
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise RequestError("invalid_tool_choice", f"tool_choice of type '{kind}' needs a name.")
        return {"type": "function", "function": {"name": names.flat(raw.get("namespace"), name)}}
    raise RequestError("invalid_tool_choice", f"tool_choice of type '{shown_name(kind)}' is not supported here.")


def _parts_text(content: Any, where: str) -> str:
    """The text of a message's content: a string, or text parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise RequestError("invalid_content", f"The content of {where} must be a string or a list of parts.")
    texts: list[str] = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind in _TEXT_PARTS and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "refusal" and isinstance(part.get("refusal"), str):
            texts.append(part["refusal"])
        else:
            raise RequestError(
                "unsupported_content_part",
                f"{where.capitalize()} has a part of type '{shown_name(kind)}'. This model reads text only.",
            )
    return "\n".join(texts)


def _output_text(content: Any) -> str:
    """The text of a function_call_output: a string, or text and image parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise RequestError("invalid_tool_output", "The output of a tool call must be a string or a list of parts.")
    texts: list[str] = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind in _TEXT_PARTS and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind in ("input_image", "image"):
            texts.append(IMAGE_LEFT_OUT)
        else:
            raise RequestError(
                "unsupported_content_part",
                f"The output of a tool call has a part of type '{shown_name(kind)}'. Only text and image parts are supported.",
            )
    return _JOIN.join(t for t in texts if t)


def _call_id(item: dict, kind: str) -> str:
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise RequestError("invalid_input_item", f"A {kind} item needs a call_id.")
    return call_id


def _assistant_call(call: dict, out: list[dict]) -> None:
    """Add one call to the last assistant turn, or start one."""
    message = normalise_message("assistant", "", tool_calls_=[call])
    last = out[-1] if out else None
    if last is not None and last["role"] == "assistant":
        out[-1] = {**last, "tool_calls": [*last.get("tool_calls", []), *message["tool_calls"]]}
    else:
        out.append(message)


def _input(raw: Any, names: ToolNames) -> tuple[list[dict], list[str]]:
    """``(messages, texts that belong in the system message)``."""
    if isinstance(raw, str):
        return [normalise_message("user", raw)], []
    if not isinstance(raw, list) or not raw:
        raise RequestError("invalid_input", "input must be a string or a non-empty list of items.")
    out: list[dict] = []
    system: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RequestError("invalid_input_item", "Each item in input must be an object.")
        kind = item.get("type") or ("message" if "role" in item else None)
        if kind == "message":
            role = item.get("role")
            if role in ("system", "developer"):
                system.append(_parts_text(item.get("content"), f"a message with role '{role}'"))
            elif role in ("user", "assistant"):
                text = _parts_text(item.get("content"), f"a {role} message")
                if role == "user" or text.strip():
                    out.append(normalise_message(role, text))
            else:
                raise RequestError("invalid_role", "Each message needs the role 'user', 'assistant', 'system' or 'developer'.")
        elif kind == "function_call":
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise RequestError("invalid_input_item", "A function_call item needs a name.")
            args = item.get("arguments")
            _assistant_call({
                "id": _call_id(item, "function_call"), "type": "function",
                "function": {"name": names.flat(item.get("namespace"), name), "arguments": args if args is not None else {}},
            }, out)
        elif kind == "custom_tool_call":
            name, text = item.get("name"), item.get("input", "")
            if not isinstance(name, str) or not name or not isinstance(text, str):
                raise RequestError("invalid_input_item", "A custom_tool_call item needs a name and a string input.")
            _assistant_call({
                "id": _call_id(item, "custom_tool_call"), "type": "function",
                "function": {"name": name, "arguments": {"input": text}},
            }, out)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            out.append(normalise_message("tool", _output_text(item.get("output")), tool_call_id=_call_id(item, kind)))
        elif kind in _DROPPED_ITEMS:
            continue
        elif kind == "item_reference":
            raise RequestError(
                "item_reference_not_supported",
                "item_reference needs a stored response, and Potluck keeps none. Send the item itself in input.",
            )
        else:
            raise RequestError("unsupported_input_item", f"An input item of type '{shown_name(kind)}' is not supported.")
    return am.join_neighbours(out), system


def _refuse_unsupported(body: dict) -> None:
    for field_name in ("previous_response_id", "conversation"):
        if body.get(field_name) is not None:
            raise RequestError(
                "previous_response_id_not_supported",
                f"{field_name} is not supported: Potluck keeps no response store. "
                "Send the full conversation in input with every request.",
            )
    if body.get("background") is True:
        raise RequestError("background_not_supported", "background responses are not supported.")
    text = body.get("text")
    fmt = text.get("format") if isinstance(text, dict) else None
    if isinstance(fmt, dict) and fmt.get("type") not in (None, "text"):
        raise RequestError(
            "unsupported_output_format",
            "Structured output (text.format) is not supported. Ask for the format in the prompt instead.",
        )


def parse_request(body: Any, *, default_temperature: float) -> ResponsesRequest:
    """Check a /v1/responses body and turn it into what the gateway routes.
    Raises ``RequestError`` with a message fit for a 400."""
    if not isinstance(body, dict):
        raise RequestError("invalid_request", "The request body must be a JSON object.")
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        raise RequestError("invalid_model", "model must be a string.")
    _refuse_unsupported(body)

    tools, names, ignored = _tools(body.get("tools"))
    messages, role_system = _input(body.get("input"), names)
    instructions = body.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise RequestError("invalid_instructions", "instructions must be a string.")
    system = _JOIN.join(t for t in [instructions or "", *role_system] if t)
    if system:
        messages.insert(0, normalise_message("system", system))
    prepared = prepare(messages, tools, _tool_choice(body.get("tool_choice"), names))

    max_tokens = body.get("max_output_tokens")
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens < 1):
        raise RequestError("invalid_max_output_tokens", "max_output_tokens must be a whole number of at least 1.")
    temperature = body.get("temperature")
    if temperature is None:
        temperature = default_temperature
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
        raise RequestError("invalid_temperature", "temperature must be a number from 0 to 2.")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise RequestError("invalid_stream", "stream must be true or false.")
    metadata = body.get("metadata")
    parallel = body.get("parallel_tool_calls")
    return ResponsesRequest(
        model=model or None, prepared=prepared, max_tokens=max_tokens, temperature=float(temperature),
        stream=stream, first_call_only=parallel is False, names=names, ignored_tools=ignored,
        echo={
            "instructions": instructions, "max_output_tokens": max_tokens,
            "metadata": metadata if isinstance(metadata, dict) else {},
            "parallel_tool_calls": parallel is not False, "temperature": float(temperature),
            "tool_choice": body.get("tool_choice") if body.get("tool_choice") is not None else "auto",
            "top_p": body["top_p"] if isinstance(body.get("top_p"), (int, float)) and not isinstance(body.get("top_p"), bool) else 1.0,
        },
    )


def prompt_text(request: ResponsesRequest) -> str:
    """The text a prompt token count is taken over (see anthropic_messages.prompt_text)."""
    return _prompt_text(request.prepared.messages, request.prepared.tools)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def error_kind(status: int) -> str:
    if status in (400, 404, 409, 413, 422):
        return "invalid_request_error"
    return {401: "authentication_error", 403: "permission_error", 429: "rate_limit_error"}.get(status, "server_error")


def error_body(status: int, message: str, code: str | None = None) -> dict:
    return {"error": {"message": message, "type": error_kind(status), "param": None, "code": code}}


def context_length_message(tokens: int, window: int) -> str:
    return f"This model's maximum context length is {window} tokens. However, your input resulted in {tokens} tokens."


# ---------------------------------------------------------------------------
# Response objects
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def new_response_id() -> str:
    return _new_id("resp")


def _usage(input_tokens: int, output_tokens: int) -> dict:
    return {
        "input_tokens": input_tokens, "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens, "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }


def _response(request: ResponsesRequest, model: str, response_id: str, created_at: int, status: str,
              output: list[dict], usage: dict | None = None, error: dict | None = None,
              incomplete: str | None = None) -> dict:
    echo = request.echo
    return {
        "id": response_id, "object": "response", "created_at": created_at, "status": status,
        "background": False, "error": error,
        "incomplete_details": {"reason": incomplete} if incomplete else None,
        "instructions": echo["instructions"], "max_output_tokens": echo["max_output_tokens"],
        "metadata": echo["metadata"], "model": model, "output": output,
        "parallel_tool_calls": echo["parallel_tool_calls"], "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None}, "store": False, "temperature": echo["temperature"],
        "text": {"format": {"type": "text"}}, "tool_choice": echo["tool_choice"], "tools": [],
        "top_p": echo["top_p"], "truncation": "disabled", "usage": usage, "user": None,
    }


def _text_part(text: str) -> dict:
    return {"type": "output_text", "text": text, "annotations": [], "logprobs": []}


def _message_item(item_id: str, text: str, status: str = "completed") -> dict:
    return {
        "id": item_id, "type": "message", "status": status, "role": "assistant",
        "content": [_text_part(text)] if status == "completed" else [],
    }


def _custom_input(arguments: str) -> str:
    """The string a freeform tool is called with, from the JSON the model wrote."""
    try:
        value = json.loads(arguments)
    except (ValueError, RecursionError):
        return arguments
    if isinstance(value, dict) and isinstance(value.get("input"), str):
        return value["input"]
    return arguments


def _call_item(item_id: str, call_id: str, flat: str, arguments: str, names: ToolNames, status: str = "completed") -> dict:
    if flat in names.custom:
        return {
            "id": item_id.replace("fc_", "ctc_", 1), "type": "custom_tool_call", "status": status,
            "call_id": call_id, "name": flat, "input": _custom_input(arguments) if status == "completed" else "",
        }
    space, name = names.split(flat)
    item = {"id": item_id, "type": "function_call", "status": status, "call_id": call_id, "name": name,
            "arguments": arguments}
    if space is not None:
        item["namespace"] = space
    return item


def _arguments_text(arguments: object) -> str:
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments if arguments is not None else {})
    except (TypeError, ValueError):
        return "{}"


def response_from_completion(result: dict, request: ResponsesRequest, model: str, input_estimate: int) -> tuple[dict, bool]:
    """``(Response object, whether any usage figure is an estimate)`` for a shaped
    chat.completion (see gateway_reply.shape_completion)."""
    choices = result.get("choices") if isinstance(result, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    reasoning, text = message.get("reasoning_content"), message.get("content")
    chars = len(reasoning) if isinstance(reasoning, str) else 0
    text = text if isinstance(text, str) else ""
    chars += len(text)

    output: list[dict] = []
    if text:
        output.append(_message_item(_new_id("msg"), text))
    calls = []
    for call in message.get("tool_calls") or ():
        function = call.get("function") if isinstance(call, dict) and isinstance(call.get("function"), dict) else {}
        if not isinstance(function.get("name"), str):
            continue
        arguments = _arguments_text(function.get("arguments"))
        chars += len(function["name"]) + len(arguments)
        call_id = call.get("id") if isinstance(call.get("id"), str) else new_call_id()
        calls.append(_call_item(_new_id("fc"), call_id, function["name"], arguments, request.names))
    if request.first_call_only:
        calls = calls[:1]
    output.extend(calls)

    usage = result.get("usage") if isinstance(result, dict) and isinstance(result.get("usage"), dict) else {}
    given_in, given_out = _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))
    cut = choice.get("finish_reason") == "length" and not calls
    reply = _response(
        request, model, new_response_id(), int(time.time()), "incomplete" if cut else "completed", output,
        _usage(input_estimate if given_in is None else given_in, tokens_for_chars(chars) if given_out is None else given_out),
        incomplete="max_output_tokens" if cut else None,
    )
    return reply, given_in is None or given_out is None


# ---------------------------------------------------------------------------
# Streamed replies
# ---------------------------------------------------------------------------


@dataclass
class _Open:
    """The output item being streamed."""

    kind: str  # "message" or "call"
    item_id: str
    index: int  # output_index
    text: str = ""  # message text so far, or call arguments so far
    call_id: str = ""
    name: str = ""
    engine_index: object = None


class _Translator:
    """Turns shaped chat.completion.chunk dicts into Responses events, one item at a
    time: an item is closed before the next opens, so items never interleave."""

    def __init__(self, request: ResponsesRequest, model: str, input_tokens: int) -> None:
        self._request = request
        self._model = model
        self._input_tokens = input_tokens
        self._id = new_response_id()
        self._created = int(time.time())
        self._seq = 0
        self._next_index = 0
        self._open: _Open | None = None
        self._output: list[dict] = []
        self._dropped: set = set()  # engine indexes of calls left out (parallel_tool_calls false)
        self._calls = 0
        self._finish: str | None = None
        self._out_tokens: int | None = None
        self._chars = 0

    def _event(self, name: str, **fields: Any) -> str:
        body = {"type": name, **fields, "sequence_number": self._seq}
        self._seq += 1
        return f"event: {name}\ndata: {json.dumps(body)}\n\n"

    def _snapshot(self, status: str, **extra: Any) -> dict:
        return _response(self._request, self._model, self._id, self._created, status,
                         self._output if status != "in_progress" else [], **extra)

    def start(self) -> list[str]:
        return [self._event("response.created", response=self._snapshot("in_progress")),
                self._event("response.in_progress", response=self._snapshot("in_progress"))]

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
        if isinstance(reasoning, str):
            self._chars += len(reasoning)  # counted for the estimate, never shown
        text = delta.get("content")
        if isinstance(text, str) and text:
            self._chars += len(text)
            out += self._text(text)
        for call in delta.get("tool_calls") or ():
            out += self._tool_call(call)
        reason = choice.get("finish_reason")
        if isinstance(reason, str) and reason:
            self._finish = reason
        return out

    def _begin(self, kind: str, item_id: str) -> _Open:
        opened = _Open(kind, item_id, self._next_index)
        self._next_index += 1
        self._open = opened
        return opened

    def _text(self, text: str) -> list[str]:
        out = []
        if self._open is None or self._open.kind != "message":
            out += self._close()
            opened = self._begin("message", _new_id("msg"))
            out.append(self._event("response.output_item.added", output_index=opened.index,
                                   item=_message_item(opened.item_id, "", "in_progress")))
            out.append(self._event("response.content_part.added", item_id=opened.item_id, output_index=opened.index,
                                   content_index=0, part=_text_part("")))
        opened = self._open
        assert opened is not None
        opened.text += text
        out.append(self._event("response.output_text.delta", item_id=opened.item_id, output_index=opened.index,
                               content_index=0, delta=text, logprobs=[]))
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
        current = self._open
        if isinstance(name, str) and name and (current is None or current.kind != "call" or index != current.engine_index):
            if self._request.first_call_only and self._calls:
                self._dropped.add(index)
                return []
            out += self._close()
            opened = self._begin("call", _new_id("fc"))
            opened.engine_index = index
            opened.name = name
            opened.call_id = call.get("id") if isinstance(call.get("id"), str) else new_call_id()
            self._calls += 1
            self._chars += len(name)
            out.append(self._event("response.output_item.added", output_index=opened.index,
                                   item=_call_item(opened.item_id, opened.call_id, name, "", self._request.names, "in_progress")))
        current = self._open
        if isinstance(arguments, str) and arguments and current is not None and current.kind == "call" and index == current.engine_index:
            self._chars += len(arguments)
            current.text += arguments
            # A freeform tool's input is the string inside the JSON, so it goes out
            # whole when the call closes. A function's arguments stream as they come.
            if current.name not in self._request.names.custom:
                out.append(self._event("response.function_call_arguments.delta", item_id=current.item_id,
                                       output_index=current.index, delta=arguments))
        return out

    def _close(self) -> list[str]:
        opened, self._open = self._open, None
        if opened is None:
            return []
        out = []
        if opened.kind == "message":
            part = _text_part(opened.text)
            out.append(self._event("response.output_text.done", item_id=opened.item_id, output_index=opened.index,
                                   content_index=0, text=opened.text, logprobs=[]))
            out.append(self._event("response.content_part.done", item_id=opened.item_id, output_index=opened.index,
                                   content_index=0, part=part))
            item = _message_item(opened.item_id, opened.text)
        else:
            arguments = opened.text or "{}"
            item = _call_item(opened.item_id, opened.call_id, opened.name, arguments, self._request.names)
            if item["type"] == "custom_tool_call":
                if item["input"]:
                    out.append(self._event("response.custom_tool_call_input.delta", item_id=item["id"],
                                           output_index=opened.index, delta=item["input"]))
                out.append(self._event("response.custom_tool_call_input.done", item_id=item["id"],
                                       output_index=opened.index, input=item["input"]))
            else:
                out.append(self._event("response.function_call_arguments.done", item_id=opened.item_id,
                                       output_index=opened.index, arguments=arguments))
        out.append(self._event("response.output_item.done", output_index=opened.index, item=item))
        self._output.append(item)
        return out

    def finish(self) -> list[str]:
        out = self._close()
        tokens = self._out_tokens if self._out_tokens is not None else tokens_for_chars(self._chars)
        usage = _usage(self._input_tokens, tokens)
        if self._finish == "length" and not self._calls:
            out.append(self._event("response.incomplete", response=self._snapshot(
                "incomplete", usage=usage, incomplete="max_output_tokens")))
        else:
            out.append(self._event("response.completed", response=self._snapshot("completed", usage=usage)))
        return out

    def fail(self, failure: Failure) -> list[str]:
        error = {"code": failure.code or "server_error", "message": failure.message}
        return [self._event("response.failed", response=self._snapshot("failed", error=error))]


class ResponseStream:
    """One streamed reply: the engine's chunks in, Responses events out.

    ``start`` waits a short while for the engine's first word, so a failure there
    can still be a plain HTTP error, never a 200 stream. If the engine is quiet
    longer than that (a long prompt takes time to process) the stream begins anyway,
    and a later failure is a ``response.failed`` event. ``events`` then yields the
    events, with a comment line whenever ``keepalive_interval`` seconds pass with
    nothing sent. The keepalive never waits for the engine and the engine's chunks
    never wait for it.

    ``describe`` turns a failure, an exception or the local engine's ``__error__``
    chunk, into a ``Failure``. Call ``aclose`` if ``events`` is never run to its end.
    """

    def __init__(
        self,
        upstream: AsyncIterator,
        request: ResponsesRequest,
        model: str,
        input_tokens: int,
        describe: Callable[[object], Failure],
        *,
        keepalive_interval: float = KEEPALIVE_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._upstream = upstream
        self._translator = _Translator(request, model, input_tokens)
        self._describe = describe
        self._interval = keepalive_interval
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
        translator = self._translator
        try:
            for event in translator.start():
                yield event
            last_sent = self._clock()
            while True:
                outcome, self._buffered = self._buffered, None
                if outcome is None:
                    if self._pending is None:
                        self._pending = asyncio.get_running_loop().create_task(self._read())
                    await asyncio.wait({self._pending}, timeout=max(self._interval - (self._clock() - last_sent), 0))
                    if not self._pending.done():
                        yield KEEPALIVE
                        last_sent = self._clock()
                        continue
                    outcome, self._pending = self._pending.result(), None
                kind, value = outcome
                if kind == "end":
                    for event in translator.finish():
                        yield event
                    return
                if kind == "failure":
                    for event in translator.fail(self._describe(value)):
                        yield event
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
