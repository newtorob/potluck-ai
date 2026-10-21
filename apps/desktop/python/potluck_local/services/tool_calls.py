"""Read tool calls out of a model's reply text.

llama-cpp-python renders ``tools`` through the model's own chat template, but it
does not read the model's tool-call text back into ``tool_calls``. This module
does that, for the formats of the models in our catalog:

  Hermes / Qwen 2.5 / Qwen 3    <tool_call>{"name": ..., "arguments": {...}}</tool_call>
                                (one block per call, several blocks allowed)
  Llama 3.1 / 3.2 JSON          {"name": ..., "parameters": {...}}, with or
                                without a leading <|python_tag|>
  Mistral (Ministral 8B)        [TOOL_CALLS] [{"name": ..., "arguments": {...}}]
  Phi-4 mini                    <|tool_call|>[{"name": ..., "arguments": {...}}]<|/tool_call|>

llama-cpp-python does not print special tokens, so in practice the last two
arrive as a bare JSON array. Both spellings are read. Only the Hermes form has
been observed in a real model run here (Qwen 2.5); the others follow the model
cards and are covered by recorded-shape unit tests.

What counts as a call
---------------------
A reply is a tool call when its tool part is made only of well-formed calls,
each naming a tool the request offered, with arguments that are a JSON object.
Anything else (bad JSON, an unknown name, too many calls, oversized arguments,
text between or after the calls) leaves the whole tool part as ordinary
content. Nothing is guessed, repaired or executed, and nothing raises.

The text comes from a model, possibly on another machine, so it is bounded:
MAX_TOOL_CALLS calls, MAX_ARGUMENT_BYTES per call, MAX_TOOL_TEXT held back per
reply. Arguments are never logged here.

Which marker text counts
------------------------
Text that spells a marker is not the marker. A file, web page or tool result the
model quotes can contain ``<tool_call>{...}</tool_call>``, and the decoded text of
that quote is the same characters as a real call. Where the loaded vocab has the
marker as a single token, the engine that decoded the reply says which markers
were sampled as that token: it puts ``AUTH`` (a private-use character) in front of
each one (see tool_markers.py). Those markers are "proven": a reply is read as a
call only when its marker arrives with ``AUTH``, and the same text without it is
content. A marker the vocab has no token for keeps being read from text. ``AUTH``
never reaches a client: the scanner removes it from everything it returns, and the
engine escapes any ``AUTH`` the model itself wrote.

Streaming
---------
``ToolCallScanner`` passes ordinary text through as it arrives. From the first
sign of a call it holds the rest back until the reply ends, then parses it. It
never lets half a marker out as text: the end of a piece that could be the
start of a marker waits for the next piece.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass

MAX_TOOL_CALLS = 16
MAX_ARGUMENT_BYTES = 131_072
MAX_TOOL_TEXT = 524_288
# Whitespace waited out before the start of a reply is decided.
MAX_LEAD = 4096

HERMES_OPEN = "<tool_call>"
HERMES_CLOSE = "</tool_call>"
_LEADS = ("<|python_tag|>", "[TOOL_CALLS]", "<|tool_call|>")
_MARKERS = (HERMES_OPEN,) + _LEADS
MARKER_TEXTS = _MARKERS
AUTH = "\ue000"
_TRAILERS = ("<|/tool_call|>", "<|eom_id|>", "<|eot_id|>", "<|im_end|>", "</s>")
# What may begin the JSON of a call. A reply that starts with "{" and then shows
# any other key is an ordinary JSON answer and is released at once.
_CALL_KEYS = ("name", "type", "function", "id", "arguments", "parameters")


def _spelled(proven: frozenset[str]) -> tuple[str, ...]:
    """The markers as they must appear in text: a proven one behind ``AUTH``."""
    return tuple(AUTH + m if m in proven else m for m in _MARKERS)


# ---------------------------------------------------------------------------
# A parsed call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # a JSON object, as text, as OpenAI returns it

    def message(self, index: int | None = None) -> dict:
        call = {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": self.arguments}}
        if index is not None:
            call = {"index": index, **call}
        return call


def new_call_id(taken: Iterable[str] = ()) -> str:
    """Nine letters and digits. Some chat templates (Mistral's) refuse any other length."""
    used = set(taken)
    while True:
        candidate = "call" + secrets.token_hex(3)[:5]
        if candidate not in used:
            return candidate


def _no_constant(_name: str):
    raise ValueError("not JSON")


_DECODER = json.JSONDecoder(parse_constant=_no_constant)


def _call_from_object(obj: object, offered: frozenset[str], taken: list[str]) -> ToolCall | None:
    if not isinstance(obj, dict):
        return None
    if obj.get("type", "function") != "function":
        return None
    fn = obj["function"] if isinstance(obj.get("function"), dict) else obj
    name = fn.get("name")
    if not isinstance(name, str) or name not in offered:
        return None
    args = fn["arguments"] if "arguments" in fn else fn.get("parameters", {})
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = _DECODER.decode(args)
        except (ValueError, RecursionError):
            return None
    if not isinstance(args, dict):
        return None
    try:
        text = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None
    text = text.replace(AUTH, "")
    if len(text.encode("utf-8", "replace")) > MAX_ARGUMENT_BYTES:
        return None
    call = ToolCall(new_call_id(taken), name, text)
    taken.append(call.id)
    return call


def _parse_json_values(text: str, offered: frozenset[str]) -> list[ToolCall] | None:
    """Calls written as JSON objects or arrays, separated by space, ";" or ","."""
    calls: list[ToolCall] = []
    taken: list[str] = []
    pos = 0
    while True:
        while pos < len(text) and (text[pos].isspace() or text[pos] in ";,"):
            pos += 1
        if pos >= len(text):
            break
        try:
            value, pos = _DECODER.raw_decode(text, pos)
        except (ValueError, RecursionError):
            return None
        for item in value if isinstance(value, list) else [value]:
            call = _call_from_object(item, offered, taken)
            if call is None:
                return None
            calls.append(call)
        if len(calls) > MAX_TOOL_CALLS:
            return None
    return calls or None


def _parse_hermes(text: str, offered: frozenset[str], proven: frozenset[str] = frozenset()) -> list[ToolCall] | None:
    calls: list[ToolCall] = []
    taken: list[str] = []
    opener = AUTH + HERMES_OPEN if HERMES_OPEN in proven else HERMES_OPEN
    pos = 0
    while pos < len(text):
        if not text.startswith(opener, pos):
            return None
        start = pos + len(opener)
        end = text.find(HERMES_CLOSE, start)
        body = text[start:] if end < 0 else text[start:end]  # a missing closing tag at the very end is allowed
        pos = len(text) if end < 0 else end + len(HERMES_CLOSE)
        parsed = _parse_json_values(body, offered)
        if parsed is None:
            return None
        calls.extend(parsed)
        if len(calls) > MAX_TOOL_CALLS:
            return None
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return calls or None


def parse_calls(text: str, offered: Iterable[str], proven: frozenset[str] = frozenset()) -> list[ToolCall] | None:
    """The calls in ``text``, which must hold only calls, or None.

    ``proven`` is the set of markers that count only behind ``AUTH`` (see the module
    docstring). With none proven, ``AUTH`` is dropped and text alone decides."""
    if not isinstance(text, str):
        return None
    names = frozenset(offered)
    if not proven:
        text = text.replace(AUTH, "")
    body = text.strip()
    stripped = True
    while stripped:
        stripped = False
        for trailer in _TRAILERS:
            if body.endswith(trailer):
                body = body[: -len(trailer)].rstrip()
                stripped = True
    spelled = dict(zip(_MARKERS, _spelled(proven)))
    if body.startswith(spelled[HERMES_OPEN]):
        return _parse_hermes(body, names, proven)
    for lead in _LEADS:
        if body.startswith(spelled[lead]):
            return _parse_json_values(body[len(spelled[lead]):], names)
    if body[:1] in ("{", "["):
        return _parse_json_values(body, names)
    return None


# ---------------------------------------------------------------------------
# Streaming: pass text through, hold a call back
# ---------------------------------------------------------------------------


def _viable_call_prefix(text: str, offered: frozenset[str]) -> bool:
    """Whether the start of a reply, seen so far, could still be a JSON call.

    ``text`` begins at "{" or "[". A "[" must be followed by "{". The first key
    must be one a call uses, and when it is "name" its value must be, or begin,
    a tool name the request offered. Everything else is an ordinary JSON answer.
    """
    s = text.lstrip()
    if s.startswith("["):
        s = s[1:].lstrip()
    if not s:
        return True
    if s[0] != "{":
        return False
    s = s[1:].lstrip()
    if not s:
        return True
    if s[0] != '"':
        return False
    end = s.find('"', 1)
    if end < 0:
        return any(key.startswith(s[1:]) for key in _CALL_KEYS)
    key = s[1:end]
    if key not in _CALL_KEYS:
        return False
    if key != "name":
        return True
    rest = s[end + 1:].lstrip()
    if not rest:
        return True
    if rest[0] != ":":
        return False
    rest = rest[1:].lstrip()
    if not rest:
        return True
    if rest[0] != '"':
        return False
    close = rest.find('"', 1)
    if close < 0:
        return any(name.startswith(rest[1:]) for name in offered)
    return rest[1:close] in offered


def _viable_marker_body(held: str, offered: frozenset[str], markers: tuple[str, ...] = _MARKERS) -> bool:
    """After a marker, a call's JSON must follow (a Hermes tag may be in prose)."""
    for marker in markers:
        if held.startswith(marker):
            rest = held[len(marker):].lstrip()
            return not rest or (rest[0] in "{[" and _viable_call_prefix(rest[:200], offered))
    return True


class _ValueEnd:
    """Finds where the first top-level JSON value in a stream of text ends."""

    def __init__(self) -> None:
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.started = False
        self.done = False

    def feed(self, text: str) -> None:
        for ch in text:
            if self.done:
                return
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif ch == "\\":
                    self.escaped = True
                elif ch == '"':
                    self.in_string = False
            elif ch == '"':
                self.in_string = True
            elif ch in "{[":
                self.depth += 1
                self.started = True
            elif ch in "}]":
                self.depth -= 1
                if self.started and self.depth <= 0:
                    self.done = True


def _overlap_any(buf: str, markers: tuple[str, ...]) -> int:
    best = 0
    for marker in markers:
        for k in range(min(len(marker) - 1, len(buf)), best, -1):
            if buf.endswith(marker[:k]):
                best = k
                break
    return best


class CallDetector:
    """Tells the reasoning splitter where a tool call may begin inside a block that never closes.

    The splitter is given this when the request offered tools (see reasoning_split rule 5).
    It uses the same tests as the scanner below, so a call it finds is one the scanner accepts.
    """

    def __init__(self, offered: Iterable[str], proven: Iterable[str] = ()) -> None:
        self._offered = frozenset(offered)
        self._proven = frozenset(proven)
        self.markers = _spelled(self._proven)

    def viable(self, rest: str) -> bool:
        return _viable_marker_body(rest, self._offered, self.markers)

    def parses(self, rest: str) -> bool:
        return parse_calls(rest, self._offered, self._proven) is not None


_PLAIN, _JSON, _TOOL, _OFF = "plain", "json", "tool", "off"
_HEAD = 400


class ToolCallScanner:
    """Passes reply text through until a tool call starts, then holds it.

    ``feed`` returns the text that is safe to hand to the client now.
    ``finish`` returns the rest and fills ``calls``. For the same text the
    emitted content and the calls are the same however the text is cut up.

    ``trim`` is how the text before a call is treated. True (the default) drops
    the whitespace between the text and the call, so "I'll look.\n<tool_call>"
    gives "I'll look.". False, for a reply the client asked to get raw, returns the
    text before the call untouched; only a reply that is nothing but whitespace
    before the call gives no content.
    """

    def __init__(self, offered: Iterable[str], *, trim: bool = True, proven: Iterable[str] = ()) -> None:
        self._offered = frozenset(offered)
        self._trim = trim
        self._proven = frozenset(proven)
        self._markers = _spelled(self._proven)
        self._mode = _PLAIN
        self._pending = ""  # plain: whitespace or the start of a marker, not yet released
        self._at_start = True  # only whitespace seen so far
        self._emitted = False  # some text has been returned to the caller
        self._lead = ""  # whitespace before a call, given back if the call turns out to be prose
        self._parts: list[str] = []  # held text; a list so a long call is not copied on every piece
        self._size = 0
        self._head = ""  # the first _HEAD characters of the held text
        self._end = _ValueEnd()
        self._checked = False
        self._settled = False  # the start of the held text has been judged
        self.calls: list[ToolCall] = []

    # -- feeding ----------------------------------------------------------

    def feed(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        if not self._proven:
            text = text.replace(AUTH, "")
        if not text:
            return ""
        if self._mode == _OFF:
            return text.replace(AUTH, "")
        out = self._hold(text) if self._mode in (_JSON, _TOOL) else self._plain(self._pending + text)
        out = out.replace(AUTH, "")
        if out:
            self._emitted = True
        return out

    def _plain(self, buf: str) -> str:
        self._pending = ""
        if self._at_start:
            stripped = buf.lstrip()
            if not stripped:
                if len(buf) > MAX_LEAD:
                    self._at_start = False
                    return buf
                self._pending = buf
                return ""
            if stripped[0] in "{[" and _viable_call_prefix(stripped[:200], self._offered):
                self._lead = buf[: len(buf) - len(stripped)]
                self._mode = _JSON
                return self._hold(stripped)
            self._at_start = False
        first = min((i for i in (buf.find(m) for m in self._markers) if i >= 0), default=-1)
        if first >= 0:
            before = buf[:first]
            if self._trim or (not self._emitted and not before.strip()):
                before = before.rstrip()
            self._lead = buf[len(before):first]  # given back if this turns out to be prose
            self._mode = _TOOL
            return before + self._hold(buf[first:])
        keep = _overlap_any(buf, self._markers)
        body = buf[: len(buf) - keep]
        if self._trim or (not self._emitted and not body.strip()):
            # Whitespace may be dropped if a call follows, so it waits. (A raw reply
            # only drops whitespace that is all there has been so far.)
            body = body.rstrip()
            if len(buf) - len(body) > MAX_LEAD + keep:  # a long run of whitespace is just text
                body = buf[: len(buf) - keep]
        self._pending = buf[len(body):]
        return body

    def _hold(self, text: str) -> str:
        self._parts.append(text)
        self._size += len(text)
        if len(self._head) < _HEAD:
            self._head = (self._head + text)[:_HEAD]
        if self._size > MAX_TOOL_TEXT:
            return self._release()
        if not self._settled:
            viable = (
                _viable_call_prefix(self._head, self._offered)
                if self._mode == _JSON
                else _viable_marker_body(self._head, self._offered, self._markers)
            )
            if not viable:
                return self._release()
            self._settled = self._size >= _HEAD
        if self._mode == _JSON and not self._checked:
            self._end.feed(text)
            if self._end.done:
                self._checked = True
                if not self._first_value_is_call():
                    return self._release()
        return ""

    def _first_value_is_call(self) -> bool:
        """Judge the first JSON value alone, so an ordinary JSON answer is released
        when it ends and not when the whole reply does."""
        text = "".join(self._parts).lstrip()
        try:
            _, end = _DECODER.raw_decode(text)
        except (ValueError, RecursionError):
            return False
        return parse_calls(text[:end], self._offered, self._proven) is not None

    def _drop_held(self) -> str:
        held = "".join(self._parts)
        self._parts, self._size, self._head = [], 0, ""
        return held

    def _release(self) -> str:
        """Give the held text back as ordinary content and stop looking for calls."""
        text = self._lead + self._drop_held()
        self._lead = ""
        self._mode = _OFF
        self._pending = ""
        return text

    # -- the end of the reply ----------------------------------------------

    def finish(self) -> str:
        if self._mode == _OFF:
            return ""
        if self._mode == _PLAIN:
            tail, self._pending = self._pending, ""
            return tail.replace(AUTH, "")
        calls = parse_calls("".join(self._parts), self._offered, self._proven)
        if calls:
            self.calls = calls
            self._drop_held()
            self._lead = ""
            self._mode = _OFF
            return ""
        return self._release().replace(AUTH, "")


def extract_tool_calls(text: str, offered: Iterable[str], *, trim: bool = True,
                       proven: Iterable[str] = ()) -> tuple[str, list[ToolCall]]:
    """``(content, calls)`` for a whole reply. Content is "" when the reply was only calls."""
    scanner = ToolCallScanner(offered, trim=trim, proven=proven)
    content = scanner.feed(text) + scanner.finish()
    return content, scanner.calls


# ---------------------------------------------------------------------------
# What the model is asked to do
# ---------------------------------------------------------------------------


def forced_schema(tools: list[dict], tool_choice: object) -> dict | None:
    """The JSON shape a forced call must have, or None when the model may choose.

    ``tool_choice`` "required" allows a call to any offered tool; a named
    function allows only that one. The shape is the same ``{"name", "arguments"}``
    object the parser reads, so a forced reply goes through the normal path.
    """
    if tool_choice == "required":
        chosen = tools
    elif isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        chosen = [t for t in tools if t.get("function", {}).get("name") == name]
    else:
        return None
    branches = [
        {
            "type": "object",
            "properties": {
                "name": {"const": t["function"]["name"]},
                "arguments": t["function"].get("parameters") or {"type": "object"},
            },
            "required": ["name", "arguments"],
        }
        for t in chosen
    ]
    if not branches:
        return None
    return branches[0] if len(branches) == 1 else {"anyOf": branches}


def completion_kwargs(tools: list[dict] | None, tool_choice: object = None) -> dict:
    """The extra arguments for llama-cpp-python's ``create_chat_completion``.

    ``tools`` go to the chat template. A forced choice also constrains the reply
    to the call shape with a grammar (``response_format``). The choice is never
    passed on as llama-cpp-python's own ``tool_choice``: its handling of a named
    function reshapes the reply, and the peer path only carries text.
    """
    if not tools or tool_choice == "none":
        return {}
    kwargs: dict = {"tools": tools}
    schema = forced_schema(tools, tool_choice)
    if schema is not None:
        kwargs["response_format"] = {"type": "json_object", "schema": schema}
    return kwargs


def check_forced_schema(tools: list[dict], tool_choice: object) -> str | None:
    """A reason the forced call cannot be built into a grammar, or None.

    Run before a request is accepted, so a parameter schema llama.cpp cannot
    express is a clear 400 and not a failure halfway through a reply.
    """
    schema = forced_schema(tools, tool_choice)
    if schema is None:
        return None
    try:
        from llama_cpp.llama_grammar import LlamaGrammar
    except Exception:  # noqa: BLE001 - nothing to check against here
        return None
    try:
        LlamaGrammar.from_json_schema(json.dumps(schema), verbose=False)
    except Exception as exc:  # noqa: BLE001
        return f"The parameter schema cannot be enforced ({type(exc).__name__})."
    return None


_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def valid_tool_name(name: object) -> bool:
    return isinstance(name, str) and _NAME.match(name) is not None
