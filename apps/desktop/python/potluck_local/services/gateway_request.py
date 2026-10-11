"""Check and normalise the parts of an OpenAI chat request that carry tools.

Turns what a client sent (message content as a string, null or text parts;
``tools``; ``tool_choice``; assistant ``tool_calls``; ``tool`` messages) into the
plain shapes the rest of the engine uses, or raises ``RequestError`` with a
message fit for a 400. Used by the gateway and by the household peer route, so
both accept the same thing.

Messages keep their old shape ({"role", "content": str}) unless a tool field is
present, so a request without tools reaches the model and other machines exactly
as it did before.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from potluck_local.services import tool_calls

MAX_TOOLS = 128
MAX_TOOLS_BYTES = 262_144
MAX_MESSAGE_TOOL_CALLS = 64
MAX_TOOL_ID_CHARS = 200


class RequestError(ValueError):
    """A request the gateway refuses. ``code`` is stable, the message is for a person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _text_of(content: Any, role: str) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind not in ("text", "input_text") or not isinstance(part.get("text"), str):
                shown = kind if isinstance(kind, str) and kind.isidentifier() else "unknown"
                raise RequestError(
                    "unsupported_content_part",
                    f"Only text content is supported. A message of role '{role}' has a '{shown}' part.",
                )
            texts.append(part["text"])
        return "\n".join(texts)
    raise RequestError("invalid_content", f"The content of a '{role}' message must be a string, null or a list of text parts.")


def _history_tool_calls(raw: Any) -> list[dict]:
    """Assistant tool_calls, with each ``arguments`` JSON string turned into the
    object chat templates expect (a template that prints a string with ``tojson``
    would otherwise print it quoted a second time)."""
    if not isinstance(raw, list) or len(raw) > MAX_MESSAGE_TOOL_CALLS:
        raise RequestError("invalid_tool_calls", "tool_calls must be a list of function calls.")
    calls = []
    for item in raw:
        fn = item.get("function") if isinstance(item, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            raise RequestError("invalid_tool_calls", "Each tool call needs a function with a name.")
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except (ValueError, RecursionError):
                raise RequestError("invalid_tool_calls", "Tool call arguments must be a JSON object.") from None
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise RequestError("invalid_tool_calls", "Tool call arguments must be a JSON object.")
        call_id = item.get("id")
        call = {"type": "function", "function": {"name": fn["name"], "arguments": args}}
        if isinstance(call_id, str):
            call = {"id": call_id[:MAX_TOOL_ID_CHARS], **call}
        calls.append(call)
    return calls


def normalise_message(role: str, content: Any, *, tool_calls_: Any = None,
                      tool_call_id: Any = None, name: Any = None) -> dict:
    message: dict = {"role": role, "content": _text_of(content, role)}
    if tool_calls_:
        message["tool_calls"] = _history_tool_calls(tool_calls_)
    if role == "tool":
        if isinstance(tool_call_id, str):
            message["tool_call_id"] = tool_call_id[:MAX_TOOL_ID_CHARS]
        if isinstance(name, str):
            message["name"] = name
    return message


def has_tool_history(messages: Iterable[dict]) -> bool:
    return any(m.get("role") == "tool" or m.get("tool_calls") for m in messages)


# ---------------------------------------------------------------------------
# Tools and tool_choice
# ---------------------------------------------------------------------------


def normalise_tools(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise RequestError("invalid_tools", "tools must be a list.")
    if len(raw) > MAX_TOOLS:
        raise RequestError("invalid_tools", f"At most {MAX_TOOLS} tools are accepted.")
    tools: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        fn = item.get("function") if isinstance(item, dict) else None
        if not isinstance(item, dict) or item.get("type") != "function" or not isinstance(fn, dict):
            raise RequestError("invalid_tools", "Only function tools are supported: {\"type\": \"function\", \"function\": {...}}.")
        name = fn.get("name")
        if not tool_calls.valid_tool_name(name):
            raise RequestError("invalid_tools", "A tool name must be 1 to 64 letters, digits, underscores, dots or dashes.")
        if name in seen:
            raise RequestError("invalid_tools", f"The tool name '{name}' appears more than once.")
        seen.add(name)
        params = fn.get("parameters")
        if params is not None and not isinstance(params, dict):
            raise RequestError("invalid_tools", f"The parameters of tool '{name}' must be a JSON schema object.")
        clean = {"name": name}
        if isinstance(fn.get("description"), str):
            clean["description"] = fn["description"]
        if params is not None:
            clean["parameters"] = params
        tools.append({"type": "function", "function": clean})
    try:
        too_big = len(json.dumps(tools)) > MAX_TOOLS_BYTES
    except (TypeError, ValueError, RecursionError):
        raise RequestError("invalid_tools", "The tools could not be read as JSON.") from None
    if too_big:
        raise RequestError("invalid_tools", "The tool definitions are too large.")
    return tools


def resolve_tool_choice(choice: Any, tools: list[dict]) -> Any:
    """"auto", "none", "required" or ``{"type": "function", "function": {"name"}}``."""
    if choice is None:
        return "auto"
    if isinstance(choice, str):
        if choice not in ("auto", "none", "required"):
            raise RequestError("invalid_tool_choice", "tool_choice must be \"auto\", \"none\", \"required\" or a named function.")
        if choice == "required" and not tools:
            raise RequestError("invalid_tool_choice", "tool_choice \"required\" needs tools.")
        return choice
    fn = choice.get("function") if isinstance(choice, dict) else None
    name = fn.get("name") if isinstance(fn, dict) else (choice.get("name") if isinstance(choice, dict) else None)
    if not isinstance(name, str) or (isinstance(choice, dict) and choice.get("type") not in (None, "function")):
        raise RequestError("invalid_tool_choice", "tool_choice must be \"auto\", \"none\", \"required\" or a named function.")
    if not any(t["function"]["name"] == name for t in tools):
        raise RequestError("invalid_tool_choice", f"tool_choice names '{name}', which is not in tools.")
    return {"type": "function", "function": {"name": name}}


@dataclass(frozen=True)
class Prepared:
    """A request after checking. ``tools`` is what the model is shown: None when
    the request offered none, or chose tool_choice "none"."""

    messages: list[dict]
    tools: list[dict] | None
    tool_choice: Any
    tool_history: bool

    @property
    def offered_names(self) -> frozenset[str]:
        return frozenset(t["function"]["name"] for t in self.tools or ())

    @property
    def needs_tool_support(self) -> bool:
        return bool(self.tools) or self.tool_history

    def runtime_kwargs(self) -> dict:
        """Extra arguments for the runtime's chat methods; empty without tools."""
        if not self.tools:
            return {}
        return {"tools": self.tools, "tool_choice": self.tool_choice}


def prepare(messages: list[dict], tools: Any, tool_choice: Any) -> Prepared:
    """``messages`` are already normalised (see ``normalise_message``)."""
    clean_tools = normalise_tools(tools) if tools else []
    choice = resolve_tool_choice(tool_choice, clean_tools)
    offered = clean_tools if clean_tools and choice != "none" else None
    if offered:
        problem = tool_calls.check_forced_schema(offered, choice)
        if problem:
            raise RequestError("invalid_tool_choice", f"tool_choice cannot be applied: {problem}")
    return Prepared(messages, offered, choice if offered else None, has_tool_history(messages))
