"""Keep a model's reasoning out of its answer text.

A thinking model writes its reasoning inside a ``<think>`` block before the
answer. The OpenAI gateway returns the reasoning as ``reasoning_content`` and
leaves only the answer in ``content``, which is what llama.cpp's server, vLLM
and DeepSeek's API do. This module does the splitting, for whole replies and
for replies that arrive in pieces.

The rules
---------
1. The block opens only at the start of the reply, after optional whitespace.
   A ``<think>`` anywhere later is ordinary text and is left alone, so an
   answer that explains the tag is not damaged.
2. Some chat templates open the block themselves, so the model's output starts
   inside it and only ``</think>`` appears. The caller says so with
   ``starts_in_reasoning=True`` (the loaded model's template tells it). Then
   everything up to the first ``</think>`` is reasoning, and a redundant
   ``<think>`` at the start is dropped.
3. The first ``</think>`` ends the block. After it, everything is answer text,
   including any later ``<think>`` or ``</think>``. Whitespace between the
   block and the answer is dropped.
4. A reply that ends before ``</think>`` is all reasoning and the answer is
   empty, whatever the engine's finish reason. The caller keeps that finish
   reason. A model that opens the block, answers, and never closes it
   (``<think>\\n\\n391`` and a clean stop) therefore has its answer in
   ``reasoning_content``. A stream has already sent that text as reasoning when
   the end arrives, so a rule that looked at the finish reason could not apply to
   streams, and the two modes would disagree. qwen3-1.7b-q4 did not do this in
   36 plain replies; it does it only in front of a tool call (rule 5).
5. Tool calls end an unterminated block. This applies only when the caller
   passes ``calls`` (the request offered tools). If the block never closes and
   a tool-call marker starts a well-formed call section that runs to the end of
   the reply, the block ends at the marker: what came before it is the
   reasoning (empty for ``<think>\\n\\n<tool_call>...``) and the marker and what
   follows go on as answer text, for the tool parser. A marker that is part of
   the reasoning, or a block that does close, changes nothing. The marker rule
   cannot tell those apart until the reply ends or ``</think>`` arrives, so a
   stream holds back the text from a plausible marker (``<tool_call>`` followed
   by ``{``) until then, and sends it as reasoning if the block closes.
6. An empty block (what Qwen3 sends with ``/no_think``) leaves no reasoning.
7. Leading whitespace of the reasoning and trailing whitespace before
   ``</think>`` are dropped, so the pieces of a stream join to exactly the value
   the whole-reply function returns, whatever the chunk boundaries.
8. ``starts_in_reasoning=None`` means the caller does not know the template
   (a reply relayed from another machine). Whole replies then also treat a
   ``</think>`` with no opener as the end of an implicit block. A stream cannot
   do that, because the reasoning has already been sent as answer text by the
   time ``</think>`` arrives, so a stream with ``None`` only splits an explicit
   ``<think>`` opener.

The text can come from another machine, so nothing here raises on any input,
and a stream holds back at most MAX_HELD characters of whitespace or tag start,
and at most MAX_TAIL characters of a possible tool call. Neither grows by copying
the whole buffer on every piece.
"""

from __future__ import annotations

from typing import Protocol

OPEN = "<think>"
CLOSE = "</think>"
REASONING = "reasoning"
CONTENT = "content"

# The most text held back while waiting to see what comes next: whitespace, or
# the start of a tag. Anything longer is released as it is.
MAX_HELD = 4096
# The most text held back after a possible tool call. Longer is reasoning.
MAX_TAIL = 524_288
# Marker positions tried when a reply ends inside a block.
MAX_TRIES = 16
# How much of the text after a marker decides whether it can be a call.
HEAD = 400

_START, _IN_BLOCK, _AFTER_BLOCK, _ANSWER = range(4)


class CallDetector(Protocol):
    """What the splitter needs to know about tool calls (see tool_calls.CallDetector)."""

    markers: tuple[str, ...]

    def viable(self, rest: str) -> bool:
        """``rest`` starts at a marker and is at most HEAD characters of what follows:
        can it still become a call?"""

    def parses(self, rest: str) -> bool:
        """``rest`` starts at a marker and runs to the end of the reply: is it only well-formed calls?"""


def _overlap(buf: str, tag: str) -> int:
    """Length of the longest end of ``buf`` that is a proper start of ``tag``."""
    for k in range(min(len(tag) - 1, len(buf)), 0, -1):
        if buf.endswith(tag[:k]):
            return k
    return 0


def _overlap_any(buf: str, tags: tuple[str, ...]) -> int:
    return max((_overlap(buf, tag) for tag in tags), default=0)


class _Tail:
    """Text held back from the first plausible tool-call marker on. Pieces are
    kept in a list so a long tail is not copied again on every piece."""

    def __init__(self, text: str, at: int, marker_len: int) -> None:
        self.parts = [text]
        self.size = len(text)
        self.at = at  # where the marker starts in the tail
        self.marker_len = marker_len
        self.head = text[at:at + HEAD]
        self.edge = text[-(len(CLOSE) - 1):]

    def add(self, text: str) -> str:
        """Add a piece; return the text in which a close tag could newly appear."""
        window = self.edge + text
        self.parts.append(text)
        self.size += len(text)
        if len(self.head) < HEAD:
            self.head = (self.head + text)[:HEAD]
        self.edge = window[-(len(CLOSE) - 1):]
        return window

    def text(self) -> str:
        return "".join(self.parts)


class ReasoningSplitter:
    """Incremental splitter. ``feed`` pieces in, get ``(kind, text)`` pairs out.

    ``kind`` is "reasoning" or "content". Call ``finish`` once when the reply
    ends to release anything still held back. ``calls`` turns on rule 5.
    """

    def __init__(self, *, starts_in_reasoning: bool | None = False, calls: CallDetector | None = None) -> None:
        self._open_known = starts_in_reasoning is True
        self._calls = calls
        self._state = _START
        self._held = ""
        self._tail: _Tail | None = None
        self._seen_reasoning = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        if not isinstance(text, str) or not text:
            return []
        out: list[tuple[str, str]] = []
        if self._tail is not None:
            self._extend_tail(text, out)
        else:
            self._advance(self._held + text, out)
        return out

    def finish(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self._tail is not None:
            tail, self._tail = self._tail, None
            self._resolve(tail, out)
        else:
            held, self._held = self._held, ""
            if self._state == _START:
                if self._open_known:
                    self._reasoning(held.rstrip(), out)
                elif held:
                    out.append((CONTENT, held))
            elif self._state == _IN_BLOCK:
                self._reasoning(held.rstrip(), out)
        self._state = _ANSWER
        return out

    def _reasoning(self, text: str, out: list[tuple[str, str]]) -> None:
        if not self._seen_reasoning:
            text = text.lstrip()
        if text:
            self._seen_reasoning = True
            out.append((REASONING, text))

    # -- rule 5 --------------------------------------------------------------

    def _find_call(self, buf: str, start: int) -> tuple[int, str]:
        """The first marker at or after ``start`` that can still be a call, as (index, marker)."""
        markers = self._calls.markers  # type: ignore[union-attr]
        nxt = {m: buf.find(m, start) for m in markers}
        while True:
            live = [(i, m) for m, i in nxt.items() if i >= 0]
            if not live:
                return -1, ""
            i, m = min(live)
            if self._calls.viable(buf[i:i + HEAD]):  # type: ignore[union-attr]
                return i, m
            nxt[m] = buf.find(m, i + len(m))

    def _extend_tail(self, text: str, out: list[tuple[str, str]]) -> None:
        tail = self._tail
        assert tail is not None
        window = tail.add(text)
        if CLOSE in window:  # the block closes: the marker was part of the reasoning
            self._tail = None
            self._advance(tail.text(), out)
        elif tail.size > MAX_TAIL or not self._calls.viable(tail.head):  # type: ignore[union-attr]
            self._tail = None  # it was prose after all: reasoning, looking on past the marker
            self._advance(tail.text(), out, search_from=tail.at + tail.marker_len)

    def _resolve(self, tail: _Tail, out: list[tuple[str, str]]) -> None:
        """The reply ended inside the block. Find where a well-formed call section starts."""
        text = tail.text()
        at, marker_len = tail.at, tail.marker_len
        for _ in range(MAX_TRIES):
            if len(text) - at <= MAX_TAIL and self._calls.parses(text[at:]):  # type: ignore[union-attr]
                self._reasoning(text[:at].rstrip(), out)
                out.append((CONTENT, text[at:]))
                return
            nxt, marker = self._find_call(text, at + marker_len)
            if nxt < 0:
                break
            at, marker_len = nxt, len(marker)
        self._reasoning(text.rstrip(), out)

    # -- the state machine -----------------------------------------------------

    def _advance(self, buf: str, out: list[tuple[str, str]], search_from: int = 0) -> None:
        self._held = ""
        while buf:
            if self._state == _START:
                stripped = buf.lstrip()
                if stripped.startswith(OPEN):
                    self._state = _IN_BLOCK
                    buf = stripped[len(OPEN):]
                    continue
                if OPEN.startswith(stripped) and len(buf) <= MAX_HELD:
                    self._held = buf  # blank so far, or the start of "<think>"
                    return
                if self._open_known:
                    self._state = _IN_BLOCK
                    continue
                self._state = _ANSWER
                out.append((CONTENT, buf))
                return
            if self._state == _IN_BLOCK:
                end = buf.find(CLOSE)
                if end >= 0:
                    self._reasoning(buf[:end].rstrip(), out)
                    self._state = _AFTER_BLOCK
                    buf = buf[end + len(CLOSE):]
                    search_from = 0
                    continue
                keep = _overlap(buf, CLOSE)
                if self._calls is not None:
                    at, marker = self._find_call(buf, search_from)
                    if at >= 0:
                        lead = buf[:at].rstrip()
                        self._reasoning(lead, out)
                        self._tail = _Tail(buf[len(lead):], at - len(lead), len(marker))
                        return
                    partial = _overlap_any(buf, self._calls.markers)
                    keep = max(keep, min(partial, max(0, len(buf) - search_from)))
                safe = buf[: len(buf) - keep]
                body = safe.rstrip()
                self._reasoning(body, out)
                held = buf[len(body):]
                if len(held) > MAX_HELD:
                    self._reasoning(held, out)
                    held = ""
                self._held = held
                return
            if self._state == _AFTER_BLOCK:
                buf = buf.lstrip()
                if buf:
                    self._state = _ANSWER
                continue
            out.append((CONTENT, buf))
            return


def split_reasoning(text: str, *, starts_in_reasoning: bool | None = False,
                    calls: CallDetector | None = None) -> tuple[str, str]:
    """``(reasoning, content)`` for a whole reply."""
    if not isinstance(text, str):
        return "", ""
    if starts_in_reasoning is None:
        starts_in_reasoning = CLOSE in text and not text.lstrip().startswith(OPEN)
    splitter = ReasoningSplitter(starts_in_reasoning=starts_in_reasoning, calls=calls)
    pieces = splitter.feed(text) + splitter.finish()
    return (
        "".join(t for kind, t in pieces if kind == REASONING),
        "".join(t for kind, t in pieces if kind == CONTENT),
    )
