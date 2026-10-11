"""Shape the reply the OpenAI gateway hands to its client.

Runs on the text the gateway is about to return, wherever it came from (this
machine's model, a household peer, a circle or the pool), because the gateway is
the only place that knows what the client asked for:

  1. reasoning inside ``<think>`` moves to ``reasoning_content``
     (``delta.reasoning_content`` when streaming), unless the request opted out
     with ``reasoning_format: "none"``;
  2. when the request offered tools, tool-call text in the answer becomes
     ``tool_calls`` and the finish reason becomes ``tool_calls``.

With neither in play the reply is returned untouched. Streamed chunks that need
no change are passed on as the same objects, so a plain reply is identical to
what it was before this module existed.

A reply from another machine is untrusted. Nothing here raises on it: if the
shaping itself fails, the reply goes on unshaped.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass

from potluck_local.services.reasoning_split import CONTENT, REASONING, ReasoningSplitter, split_reasoning
from potluck_local.services.tool_calls import CallDetector, ToolCall, ToolCallScanner, extract_tool_calls

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShapeOptions:
    split_reasoning: bool = True
    # True/False when the loaded model's template says; None for a remote reply.
    starts_in_reasoning: bool | None = False
    offered: frozenset[str] | None = None  # tool names; None when no tools were offered

    @property
    def active(self) -> bool:
        return self.split_reasoning or bool(self.offered)


# ---------------------------------------------------------------------------
# Whole replies
# ---------------------------------------------------------------------------


def shape_completion(result: dict, options: ShapeOptions) -> dict:
    """Shape a non-streaming chat.completion in place and return it."""
    if not options.active or not isinstance(result, dict):
        return result
    try:
        choices = result.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = choice.get("message") if isinstance(choice, dict) else None
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str):
            return result
        reasoning = ""
        if options.split_reasoning:
            reasoning, text = split_reasoning(
                text, starts_in_reasoning=options.starts_in_reasoning,
                calls=CallDetector(options.offered) if options.offered else None,
            )
        calls: list[ToolCall] = []
        if options.offered:
            text, calls = extract_tool_calls(text, options.offered, trim=options.split_reasoning)
        message["content"] = text if (text or not calls) else None
        if reasoning:
            message["reasoning_content"] = reasoning
        if calls:
            message["tool_calls"] = [call.message() for call in calls]
            if choice.get("finish_reason") in (None, "stop"):
                choice["finish_reason"] = "tool_calls"
    except Exception as exc:  # noqa: BLE001 - never turn a reply into an error
        logger.warning("reply_shaping_failed error=%s", type(exc).__name__)
    return result


# ---------------------------------------------------------------------------
# Streamed replies
# ---------------------------------------------------------------------------


class _Pipeline:
    def __init__(self, options: ShapeOptions) -> None:
        self.splitter = (
            ReasoningSplitter(
                starts_in_reasoning=options.starts_in_reasoning,
                calls=CallDetector(options.offered) if options.offered else None,
            )
            if options.split_reasoning else None
        )
        # A raw reply (no reasoning split) also keeps the text before a call untouched.
        self.scanner = ToolCallScanner(options.offered, trim=options.split_reasoning) if options.offered else None

    def _through_tools(self, pieces: list[tuple[str, str]]) -> list[tuple[str, str]]:
        if self.scanner is None:
            return pieces
        out = []
        for kind, text in pieces:
            if kind == CONTENT:
                text = self.scanner.feed(text)
            if text:
                out.append((kind, text))
        return out

    def feed(self, text: str) -> list[tuple[str, str]]:
        pieces = self.splitter.feed(text) if self.splitter else [(CONTENT, text)]
        return self._through_tools(pieces)

    def finish(self) -> tuple[list[tuple[str, str]], list[ToolCall]]:
        pieces = self._through_tools(self.splitter.finish() if self.splitter else [])
        calls: list[ToolCall] = []
        if self.scanner is not None:
            tail = self.scanner.finish()
            if tail:
                pieces.append((CONTENT, tail))
            calls = self.scanner.calls
        return pieces, calls


def _derive(chunk: dict, choice: dict, delta: dict, finish: str | None, *, last: bool) -> dict:
    out = {k: v for k, v in chunk.items() if k != "choices" and (last or k != "usage")}
    out["choices"] = [{**choice, "delta": delta, "finish_reason": finish}]
    return out


async def shape_stream(source: AsyncIterator, options: ShapeOptions) -> AsyncIterator:
    """Yield the chunks of ``source`` with reasoning and tool calls shaped.

    Chunk dicts with an ``__error__`` key, and anything that is not a normal
    chunk, pass through unchanged. If shaping fails, the rest of the stream
    passes through unshaped.
    """
    pipe: _Pipeline | None = _Pipeline(options) if options.active else None
    last_seen: tuple[dict, dict] | None = None
    finished = False
    async with aclosing(source) as chunks:
        async for chunk in chunks:
            if pipe is None or finished or not isinstance(chunk, dict) or "__error__" in chunk:
                yield chunk
                continue
            try:
                shaped, finished = _shape_chunk(pipe, chunk)
            except Exception as exc:  # noqa: BLE001
                logger.warning("reply_shaping_failed error=%s", type(exc).__name__)
                pipe = None
                yield chunk
                continue
            choices = chunk.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                last_seen = (chunk, choices[0])
            for item in shaped:
                yield item
        if pipe is not None and not finished and last_seen is not None:
            # The source ended without a finish chunk: release what is held.
            try:
                pieces, calls = pipe.finish()
                for item in _emit(last_seen[0], last_seen[1], {}, pieces, calls, None, final=False):
                    yield item
            except Exception as exc:  # noqa: BLE001
                logger.warning("reply_shaping_failed error=%s", type(exc).__name__)


def _emit(chunk: dict, choice: dict, rest: dict, pieces: list[tuple[str, str]],
          calls: list[ToolCall], finish: str | None, *, final: bool) -> list[dict]:
    """The chunks for ``pieces`` and ``calls``, then (when ``final``) the closing
    chunk. ``rest`` is the upstream delta without its content; a role in it goes
    out once, on the first chunk."""
    out: list[dict] = []
    role = {"role": rest["role"]} if "role" in rest else {}
    for kind, text in pieces:
        key = "reasoning_content" if kind == REASONING else "content"
        out.append(_derive(chunk, choice, {**role, key: text}, None, last=False))
        role = {}
    for index, call in enumerate(calls):
        out.append(_derive(chunk, choice, {**role, "tool_calls": [call.message(index)]}, None, last=False))
        role = {}
    if final:
        if calls and finish in ("stop", None):
            finish = "tool_calls"
        delta = {**{k: v for k, v in rest.items() if k != "role"}, **role}
        out.append(_derive(chunk, choice, delta, finish, last=True))
    elif role and not out:
        out.append(_derive(chunk, choice, role, None, last=False))
    return out


def _shape_chunk(pipe: _Pipeline, chunk: dict) -> tuple[list[dict], bool]:
    """``(chunks to send, whether this chunk carried the finish reason)``."""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return [chunk], False
    choice = choices[0]
    delta = choice.get("delta")
    delta = delta if isinstance(delta, dict) else {}
    finish = choice.get("finish_reason")
    content = delta.get("content")
    has_text = isinstance(content, str) and bool(content)
    pieces = pipe.feed(content) if has_text else []
    calls: list[ToolCall] = []
    if finish is not None:
        tail, calls = pipe.finish()
        pieces += tail
    unchanged = not calls and (pieces == [(CONTENT, content)] if has_text else not pieces)
    if unchanged:
        return [chunk], finish is not None  # the same object, byte for byte
    rest = {k: v for k, v in delta.items() if k != "content"}
    return _emit(chunk, choice, rest, pieces, calls, finish, final=finish is not None), finish is not None
